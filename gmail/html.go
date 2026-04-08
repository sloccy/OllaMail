package gmail

import (
	"strings"
)

// extractText strips HTML tags from src and returns plain text.
// It skips content inside <script> and <style> elements.
func extractText(src string) string {
	var sb strings.Builder
	inTag := false
	inScript := false
	inStyle := false
	i := 0

	lower := strings.ToLower(src)

	for i < len(src) {
		if !inTag && !inScript && !inStyle {
			if src[i] == '<' {
				inTag = true
				// Peek at tag name
				rest := lower[i:]
				if strings.HasPrefix(rest, "<script") {
					inScript = true
				} else if strings.HasPrefix(rest, "<style") {
					inStyle = true
				}
				i++
				continue
			}
			sb.WriteByte(src[i])
			i++
			continue
		}

		if inScript {
			if idx := strings.Index(lower[i:], "</script>"); idx >= 0 {
				i += idx + len("</script>")
				inScript = false
				inTag = false
			} else {
				i = len(src)
			}
			continue
		}

		if inStyle {
			if idx := strings.Index(lower[i:], "</style>"); idx >= 0 {
				i += idx + len("</style>")
				inStyle = false
				inTag = false
			} else {
				i = len(src)
			}
			continue
		}

		if src[i] == '>' {
			inTag = false
			sb.WriteByte('\n')
		}
		i++
	}

	// Collapse whitespace lines
	lines := strings.Split(sb.String(), "\n")
	var out []string
	for _, l := range lines {
		t := strings.TrimSpace(l)
		if t != "" {
			out = append(out, t)
		}
	}
	return strings.Join(out, "\n")
}

// truncate returns s truncated to maxChars bytes (not runes, matching Python behaviour).
func truncate(s string, maxChars int) string {
	if len(s) <= maxChars {
		return s
	}
	return s[:maxChars]
}
