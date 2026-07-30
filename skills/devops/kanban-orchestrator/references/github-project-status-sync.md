# GitHub Project Board Status Sync

When an issue is closed (via PR or manually) but the **GitHub Project board** still shows it as **"In progress"**, the board status is a separate field from the issue state. The `feature-close` skill should handle this automatically, but if it's missed, fix it manually.

## Finding the project item for an issue

```bash
gh api graphql -f query='
query {
  repository(owner: "OWNER", name: "REPO") {
    issue(number: ISSUE_NUMBER) {
      projectItems(first: 10) {
        nodes {
          id
          project { title }
          fieldValueByName(name: "Status") {
            ... on ProjectV2ItemFieldSingleSelectValue {
              name
              optionId
            }
          }
        }
      }
    }
  }
}'
```

## Finding the "Done" option ID

```bash
gh project field-list PROJECT_NUMBER --owner OWNER --format json | \
  python3 -c "
import sys, json
data = json.load(sys.stdin)
for field in data['fields']:
    if field['name'] == 'Status':
        for opt in field.get('options', []):
            print(f\"{opt['name']}: {opt['id']}\")
"
```

## Updating the status to "Done"

```bash
gh api graphql -f query='
mutation {
  updateProjectV2ItemFieldValue(
    input: {
      projectId: "PROJECT_ID"
      itemId: "ITEM_ID"
      fieldId: "FIELD_ID"
      value: {
        singleSelectOptionId: "DONE_OPTION_ID"
      }
    }
  ) {
    projectV2Item { id }
  }
}'
```

## One-liner (using gh CLI only)

```bash
# Get project item ID from issue
ITEM_ID=$(gh api graphql -f query='
query { repository(owner: "my-org", name: "MyProject") {
  issue(number: ISSUE) { projectItems(first: 1) { nodes { id } } }
}' --jq '.data.repository.issue.projectItems.nodes[0].id')

# Get project ID
PROJECT_ID=$(gh api graphql -f query='
query { repository(owner: "my-org", name: "MyProject") {
  issue(number: ISSUE) { projectItems(first: 1) { nodes { project { id } } } }
}' --jq '.data.repository.issue.projectItems.nodes[0].project.id')

# Get field IDs
FIELD_ID=$(gh project field-list PROJECT_NUM --owner my-org --format json | \
  python3 -c "import sys,json;d=json.load(sys.stdin);[print(f['id']) for f in d['fields'] if f['name']=='Status']")

DONE_ID=$(gh project field-list PROJECT_NUM --owner my-org --format json | \
  python3 -c "import sys,json;d=json.load(sys.stdin);[print(o['id']) for f in d['fields'] if f['name']=='Status' for o in f['options'] if o['name']=='Done']")

# Update status
gh api graphql -f query="
mutation {
  updateProjectV2ItemFieldValue(input: {
    projectId: \"$PROJECT_ID\"
    itemId: \"$ITEM_ID\"
    fieldId: \"$FIELD_ID\"
    value: { singleSelectOptionId: \"$DONE_ID\" }
  }) { projectV2Item { id } }
}"
```

## When to check

- After closing an issue that was tracked on a project board
- After a PR merge with `Closes #N` — the issue closes but the board status may not update
- During `feature-close` execution — verify the project item status changed to "Done"

## Quick Reference

| Action | Command |
|--------|---------|
| List project fields | `gh project field-list PROJECT_NUM --owner OWNER --format json` |
| List project items | `gh project item-list PROJECT_NUM --owner OWNER --limit 100 --format json` |
| Find issue's project item | GraphQL query on `issue.projectItems` |
| Update status to Done | GraphQL mutation `updateProjectV2ItemFieldValue` |