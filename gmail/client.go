package gmail

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math/rand"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"

	"golang.org/x/oauth2"
	"google.golang.org/api/gmail/v1"
	"google.golang.org/api/option"
)

const (
	LabelInbox  = "INBOX"
	LabelUnread = "UNREAD"
	LabelSpam   = "SPAM"
	LabelTrash  = "TRASH"
)

// retryTransport wraps an http.RoundTripper with retry logic for 429 and 5xx.
type retryTransport struct {
	base http.RoundTripper
}

func (t *retryTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	var resp *http.Response
	var err error
	backoff := time.Second
	for attempt := range 3 {
		if attempt > 0 {
			jitter := time.Duration(rand.Int63n(int64(backoff / 2)))
			time.Sleep(backoff + jitter)
			backoff *= 2
		}
		resp, err = t.base.RoundTrip(req)
		if err != nil {
			continue
		}
		if resp.StatusCode == 429 || resp.StatusCode >= 500 {
			_ = resp.Body.Close()
			continue
		}
		return resp, nil
	}
	return resp, err
}

// NewService creates an authenticated Gmail API service for the given stored credentials JSON.
// If the token is refreshed, onRefresh is called with the new token JSON.
func NewService(ctx context.Context, credJSON string, oauthCfg *oauth2.Config, onRefresh func(string)) (*gmail.Service, error) {
	token, err := TokenFromJSON(credJSON)
	if err != nil {
		return nil, fmt.Errorf("parse token: %w", err)
	}

	ts := &refreshingTokenSource{
		base:      oauthCfg.TokenSource(ctx, token),
		original:  token,
		onRefresh: onRefresh,
	}

	httpClient := &http.Client{
		Transport: &retryTransport{
			base: &oauth2.Transport{
				Source: ts,
				Base:   http.DefaultTransport,
			},
		},
	}

	svc, err := gmail.NewService(ctx, option.WithHTTPClient(httpClient))
	if err != nil {
		return nil, err
	}
	return svc, nil
}

// refreshingTokenSource calls onRefresh when the token changes.
type refreshingTokenSource struct {
	mu        sync.Mutex
	base      oauth2.TokenSource
	original  *oauth2.Token
	onRefresh func(string)
}

func (s *refreshingTokenSource) Token() (*oauth2.Token, error) {
	t, err := s.base.Token()
	if err != nil {
		return nil, err
	}
	s.mu.Lock()
	changed := s.original == nil || t.AccessToken != s.original.AccessToken
	s.original = t
	s.mu.Unlock()
	if changed && s.onRefresh != nil {
		marshalToken(t, s.onRefresh)
	}
	return t, nil
}

func marshalToken(t *oauth2.Token, fn func(string)) {
	if b, err := json.Marshal(t); err == nil {
		fn(string(b))
	}
}

// ServiceWrapper holds a Gmail service and its underlying OAuth token source.
type ServiceWrapper struct {
	Svc *gmail.Service
}

// Label is a Gmail label id/name pair.
type Label struct {
	ID   string
	Name string
}

// ListLabels returns all labels for the account, sorted by name.
func ListLabels(ctx context.Context, svc *gmail.Service) ([]Label, error) {
	res, err := svc.Users.Labels.List("me").Context(ctx).Do()
	if err != nil {
		return nil, err
	}
	labels := make([]Label, 0, len(res.Labels))
	for _, l := range res.Labels {
		labels = append(labels, Label{ID: l.Id, Name: l.Name})
	}
	sort.Slice(labels, func(i, j int) bool { return labels[i].Name < labels[j].Name })
	return labels, nil
}

// BuildLabelCache returns a map of label name -> label ID, creating missing labels.
func BuildLabelCache(ctx context.Context, svc *gmail.Service, needed []string) (map[string]string, error) {
	existing, err := ListLabels(ctx, svc)
	if err != nil {
		return nil, err
	}
	cache := make(map[string]string, len(existing))
	for _, l := range existing {
		cache[l.Name] = l.ID
	}
	for _, name := range needed {
		if _, ok := cache[name]; ok {
			continue
		}
		created, err := svc.Users.Labels.Create("me", &gmail.Label{Name: name}).Context(ctx).Do()
		if err != nil {
			return nil, fmt.Errorf("create label %q: %w", name, err)
		}
		cache[name] = created.Id
	}
	return cache, nil
}

// EnsureLabel creates a label if it doesn't exist. Safe to call concurrently.
func EnsureLabel(ctx context.Context, svc *gmail.Service, name string) error {
	labels, err := ListLabels(ctx, svc)
	if err != nil {
		return err
	}
	for _, l := range labels {
		if l.Name == name {
			return nil
		}
	}
	_, err = svc.Users.Labels.Create("me", &gmail.Label{Name: name}).Context(ctx).Do()
	return err
}

// ListRecentMessageIDs returns message IDs from the inbox for the last lookbackHours hours.
func ListRecentMessageIDs(ctx context.Context, svc *gmail.Service, lookbackHours int, maxResults int64) ([]string, error) {
	after := time.Now().UTC().Add(-time.Duration(lookbackHours) * time.Hour)
	q := fmt.Sprintf("in:inbox after:%d", after.Unix())
	return paginateMessageIDs(ctx, svc, q, maxResults, 0)
}

func paginateMessageIDs(ctx context.Context, svc *gmail.Service, q string, maxResults int64, maxPages int) ([]string, error) {
	var ids []string
	var pageToken string
	page := 0
	for {
		call := svc.Users.Messages.List("me").Q(q).MaxResults(maxResults).Context(ctx)
		if pageToken != "" {
			call = call.PageToken(pageToken)
		}
		res, err := call.Do()
		if err != nil {
			return nil, err
		}
		for _, m := range res.Messages {
			ids = append(ids, m.Id)
		}
		pageToken = res.NextPageToken
		page++
		if pageToken == "" || (maxPages > 0 && page >= maxPages) {
			break
		}
	}
	return ids, nil
}

// Message is a simplified view of a Gmail message.
type Message struct {
	ID      string
	Sender  string
	Subject string
	Body    string
	Snippet string
}

// IterMessageDetails fetches full message details concurrently (up to 10 at a time).
func IterMessageDetails(ctx context.Context, svc *gmail.Service, ids []string, maxBodyChars int) (<-chan Message, <-chan error) {
	msgCh := make(chan Message, len(ids))
	errCh := make(chan error, 1)

	go func() {
		defer close(msgCh)
		defer close(errCh)

		sem := make(chan struct{}, 10)
		var wg sync.WaitGroup
		var mu sync.Mutex
		var firstErr error

		for _, id := range ids {
			wg.Add(1)
			sem <- struct{}{}
			go func() {
				defer wg.Done()
				defer func() { <-sem }()

				msg, err := fetchMessage(ctx, svc, id, maxBodyChars)
				mu.Lock()
				defer mu.Unlock()
				if err != nil {
					if firstErr == nil {
						firstErr = err
					}
					return
				}
				select {
				case msgCh <- msg:
				case <-ctx.Done():
				}
			}()
		}
		wg.Wait()
		if firstErr != nil {
			errCh <- firstErr
		}
	}()
	return msgCh, errCh
}

func fetchMessage(ctx context.Context, svc *gmail.Service, id string, maxBodyChars int) (Message, error) {
	m, err := svc.Users.Messages.Get("me", id).Format("full").Context(ctx).Do()
	if err != nil {
		return Message{}, err
	}
	msg := Message{
		ID:      id,
		Snippet: m.Snippet,
	}
	for _, h := range m.Payload.Headers {
		switch h.Name {
		case "From":
			msg.Sender = h.Value
		case "Subject":
			msg.Subject = h.Value
		}
	}
	msg.Body = extractPayloadBody(m.Payload, maxBodyChars)
	return msg, nil
}

func extractPayloadBody(payload *gmail.MessagePart, maxChars int) string {
	if payload == nil {
		return ""
	}
	return truncate(extractBodyRecursive(payload, maxChars*10), maxChars)
}

func extractBodyRecursive(part *gmail.MessagePart, maxChars int) string {
	if part == nil {
		return ""
	}
	mimeType := strings.ToLower(part.MimeType)

	if len(part.Parts) == 0 {
		// Leaf node
		if part.Body == nil || part.Body.Data == "" {
			return ""
		}
		data, err := base64.URLEncoding.DecodeString(part.Body.Data)
		if err != nil {
			data, err = base64.StdEncoding.DecodeString(part.Body.Data)
			if err != nil {
				return ""
			}
		}
		text := string(data)
		if strings.Contains(mimeType, "html") {
			text = extractText(text)
		}
		return truncate(text, maxChars)
	}

	// Prefer text/plain part
	for _, p := range part.Parts {
		if strings.Contains(strings.ToLower(p.MimeType), "plain") {
			if t := extractBodyRecursive(p, maxChars); t != "" {
				return t
			}
		}
	}
	// Fallback to any part
	for _, p := range part.Parts {
		if t := extractBodyRecursive(p, maxChars); t != "" {
			return t
		}
	}
	return ""
}

// Modify represents a set of label changes to apply to messages.
type Modify struct {
	MessageIDs   []string
	AddLabels    []string
	RemoveLabels []string
}

// BatchModifyEmails applies label changes, grouped by identical add/remove sets.
func BatchModifyEmails(ctx context.Context, svc *gmail.Service, mods []Modify) error {
	type key struct{ add, remove string }
	grouped := make(map[key]*gmail.BatchModifyMessagesRequest)

	for _, m := range mods {
		add := strings.Join(m.AddLabels, ",")
		remove := strings.Join(m.RemoveLabels, ",")
		k := key{add, remove}
		if _, ok := grouped[k]; !ok {
			grouped[k] = &gmail.BatchModifyMessagesRequest{
				AddLabelIds:    m.AddLabels,
				RemoveLabelIds: m.RemoveLabels,
			}
		}
		grouped[k].Ids = append(grouped[k].Ids, m.MessageIDs...)
	}

	for _, req := range grouped {
		for len(req.Ids) > 0 {
			batch := req.Ids
			if len(batch) > 1000 {
				batch = batch[:1000]
			}
			if err := svc.Users.Messages.BatchModify("me", &gmail.BatchModifyMessagesRequest{
				Ids:            batch,
				AddLabelIds:    req.AddLabelIds,
				RemoveLabelIds: req.RemoveLabelIds,
			}).Context(ctx).Do(); err != nil {
				return err
			}
			req.Ids = req.Ids[len(batch):]
		}
	}
	return nil
}

// BatchTrashEmails moves messages to trash.
func BatchTrashEmails(ctx context.Context, svc *gmail.Service, ids []string) error {
	for len(ids) > 0 {
		batch := ids
		if len(batch) > 1000 {
			batch = batch[:1000]
		}
		if err := svc.Users.Messages.BatchModify("me", &gmail.BatchModifyMessagesRequest{
			Ids:            batch,
			AddLabelIds:    []string{LabelTrash},
			RemoveLabelIds: []string{LabelInbox},
		}).Context(ctx).Do(); err != nil {
			return err
		}
		ids = ids[len(batch):]
	}
	return nil
}

// FetchEmailsOlderThan returns message IDs older than `days` days with the given label,
// excluding any labels in excludeLabels.
func FetchEmailsOlderThan(ctx context.Context, svc *gmail.Service, days int, label string, excludeLabels []string, maxPages int) ([]string, error) {
	before := time.Now().UTC().AddDate(0, 0, -days)
	var sb strings.Builder
	fmt.Fprintf(&sb, "before:%s", before.Format("2006/01/02"))
	if label != "" {
		sb.WriteString(" label:")
		sb.WriteString(label)
	}
	for _, ex := range excludeLabels {
		sb.WriteString(" -label:")
		sb.WriteString(ex)
	}
	return paginateMessageIDs(ctx, svc, sb.String(), 500, maxPages)
}
