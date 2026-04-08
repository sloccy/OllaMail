package main

import (
	"encoding/json"
	"fmt"
	"html/template"
	"time"
)

// tmplFuncs returns the template FuncMap used by all templates.
func tmplFuncs() template.FuncMap {
	return template.FuncMap{
		"fmtdate":        fmtdate,
		"fmtdateStacked": fmtdateStacked,
		"fmtinterval":    fmtinterval,
		"fmtretention":   fmtretention,
		"toJSON":         toJSON,
		"safeHTML":       func(s string) template.HTML { return template.HTML(s) },
		"printf":         fmt.Sprintf,
		"dict":           dict,
		"not":            func(b bool) bool { return !b },
	}
}

// dict creates a map from alternating key/value pairs, used in templates as (dict "Key" val ...).
func dict(pairs ...any) map[string]any {
	m := make(map[string]any, len(pairs)/2)
	for i := 0; i+1 < len(pairs); i += 2 {
		key, _ := pairs[i].(string)
		m[key] = pairs[i+1]
	}
	return m
}

const tsLayout = "2006-01-02 15:04:05"

func parseTS(ts string) (time.Time, bool) {
	if ts == "" {
		return time.Time{}, false
	}
	// Handle sql.NullString wrapper — ts may arrive as a struct; callers should pass .String
	t, err := time.Parse(tsLayout, ts)
	if err != nil {
		return time.Time{}, false
	}
	return t, true
}

func fmtdate(ts string) string {
	t, ok := parseTS(ts)
	if !ok {
		return "--"
	}
	return t.Format("2 Jan, 15:04")
}

func fmtdateStacked(ts string) template.HTML {
	t, ok := parseTS(ts)
	if !ok {
		return template.HTML("--")
	}
	date := t.Format("2 Jan")
	timeStr := t.Format("15:04")
	return template.HTML(date + `<br><span class="text-muted">` + timeStr + `</span>`)
}

func fmtinterval(secs int) string {
	switch {
	case secs >= 3600:
		return fmt.Sprintf("%dh", secs/3600)
	case secs >= 60:
		return fmt.Sprintf("%dm", secs/60)
	default:
		return fmt.Sprintf("%ds", secs)
	}
}

func fmtretention(days int64) string {
	if days >= 365 && days%365 == 0 {
		y := days / 365
		if y == 1 {
			return "1 year"
		}
		return fmt.Sprintf("%d years", y)
	}
	if days == 1 {
		return "1 day"
	}
	return fmt.Sprintf("%d days", days)
}

func toJSON(v any) template.JS {
	b, err := json.Marshal(v)
	if err != nil {
		return template.JS("null")
	}
	return template.JS(b)
}

// loadTemplates parses all templates from disk.
func loadTemplates() (*template.Template, error) {
	t := template.New("").Funcs(tmplFuncs())

	// Parse partials (defines action_checkboxes, etc.)
	if _, err := t.ParseFiles("templates/partials.html"); err != nil {
		return nil, fmt.Errorf("parse partials: %w", err)
	}

	// Parse all fragment files (both {{define}} blocks and standalone fragments).
	fragmentFiles := []string{
		"templates/fragments/logs_list.html",
		"templates/fragments/prompt_card_view.html",
		"templates/fragments/prompt_card_edit.html",
		"templates/fragments/dashboard.html",
		"templates/fragments/accounts_list.html",
		"templates/fragments/prompts_list.html",
		"templates/fragments/settings_form.html",
		"templates/fragments/history_filters.html",
		"templates/fragments/history_table.html",
		"templates/fragments/retention_panel.html",
		"templates/fragments/account_options.html",
		"templates/fragments/oauth_step2.html",
	}
	for _, f := range fragmentFiles {
		if _, err := t.ParseFiles(f); err != nil {
			return nil, fmt.Errorf("parse %s: %w", f, err)
		}
	}

	// Parse top-level page template
	if _, err := t.ParseFiles("templates/index.html"); err != nil {
		return nil, fmt.Errorf("parse index: %w", err)
	}

	return t, nil
}
