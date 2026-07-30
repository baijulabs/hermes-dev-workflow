# Patch Tool Indentation Issue: Workaround

## Problem

The `patch` tool can incorrectly match `old_string` when the string appears in multiple similar contexts, and can add unwanted indentation. This happened with a file that has CRLF line endings (`\r\n`) — the patch tool resolved the match inside a different function's indented context, wrapping the replacement content with extra whitespace.

## Symptoms

- `Found 297 matches for old_string` — even though the text looks unique to you
- Replacement content gets shifted 4-12 spaces to the right (extra indentation)
- `IndentationError: unexpected indent` when the file is checked afterward

## Root Cause

Files with Windows-style line endings (`\r\n`) can confuse the fuzzy matching, causing it to match inside an indented block rather than at the top level.

## Workaround: Python Inline Replacement

When `patch` consistently mis-matches, use a Python one-liner in `terminal`:

```bash
python3 -c "
with open('path/to/file.py') as f:
    content = f.read()

old = 'exact_string_to_find_with_context'
new = 'replacement_string_with_context'

count = content.count(old)
print(f'Found {count} occurrences')
if count == 1:
    content = content.replace(old, new)
    with open('path/to/file.py', 'w') as f:
        f.write(content)
    print('Replaced successfully')
"
```

## Alternatively: execute_code

For multi-step replacements or conditional logic, use `execute_code`. **Prefer `terminal` + `python3 -c` for simple find-and-replace** — it's faster (no sandbox spin-up) and avoids f-string syntax traps (`f-string expression part cannot include a backslash`).

```python
with open('path/to/file.py') as f:
    content = f.read()

# Simple replacement
content = content.replace(old1, new1)

# Verify before writing
if content.count('assert.*status_code.*403') == 0:
    with open('path/to/file.py', 'w') as f:
        f.write(content)
```

## Pitfalls

- The `old` string MUST be exact — include leading whitespace, surrounding context
- Use `content.count(old)` to verify uniqueness before replacement
- Always re-verify the file after writing (e.g., `grep -n "expected_content" file`)
- Files with CRLF line endings (`\r\n`) are more susceptible to patch mis-matching; convert with `sed -i 's/\r$//'` before patching
- The `cat -A` command reveals line endings: `$` = LF, `^M$` = CRLF