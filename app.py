import json
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

app = Flask(__name__)
DATA_DIR = Path(__file__).parent / "data"
MAPPINGS_FILE = DATA_DIR / "mappings.json"
NUMERIC_CACHE_FILE  = DATA_DIR / "numeric_cache.json"
VARIANCE_MAP_FILE   = DATA_DIR / "variance_map.json"
REPORT_CONFIG_FILE  = DATA_DIR / "report_config.json"
SYNC_LOG_FILE       = DATA_DIR / "sync_log.json"

# Numeric task_type → dashboard type
NUMERIC_TYPE_MAP = {
    "flux": "flux",
    "rec_prepare_account": "reconciliation",
    "custom": "checklist",
    "journal_entry": "checklist",
}

# Dashboard display labels for task types
TYPE_LABELS = {
    "flux":          "Flux",
    "reconciliation": "Reconciliation",
    "checklist":     "Checklist",
    "review_note":   "Review Note",
}


def load_mappings():
    """Load custom name/team mappings from JSON file."""
    if MAPPINGS_FILE.exists():
        try:
            with open(MAPPINGS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"names": {}, "teams": {}}


def save_mappings(mappings):
    """Save custom name/team mappings to JSON file."""
    with open(MAPPINGS_FILE, "w") as f:
        json.dump(mappings, f, indent=2)


def load_variance_map():
    """Load account variance data for flux threshold filtering."""
    if VARIANCE_MAP_FILE.exists():
        try:
            with open(VARIANCE_MAP_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def load_report_config():
    """Load per-report threshold config."""
    if REPORT_CONFIG_FILE.exists():
        try:
            with open(REPORT_CONFIG_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"ignored_reports": [], "mom_reports": [],
            "thresholds": {"default": {"dollar": 500000, "pct": 0.10},
                           "mom_account": {"dollar": 50000, "pct": 0.10},
                           "mom_total":   {"dollar": 500000, "pct": 0.10}}}


def is_flux_required(task_name, report_id, key_id, variance_map, report_config):
    """Return True if a flux task requires explanation based on its variance and report thresholds.

    Rules:
      - Ignored reports            → always skip
      - MoM reports, account line  → $50k AND 10%
      - MoM reports, total line    → $500k AND 10%
      - All other reports          → $500k AND 10%

    Looks up variance by key_id (the exact group/cost-center instance) so that
    when the same account appears in multiple cost centers with different variances,
    each task is evaluated against its own variance — not the total or a random instance.
    """
    if not variance_map:
        return True

    ignored = set(report_config.get("ignored_reports", []))
    mom     = set(report_config.get("mom_reports", []))
    thresh  = report_config.get("thresholds", {})

    if report_id in ignored:
        return False

    # Primary lookup: by key_id (exact cost-center instance)
    entry = variance_map.get(key_id)
    if not entry:
        return True  # unknown task — include by default

    effective_report_id = entry.get("report_id", report_id)
    if effective_report_id in mom:
        # key_id format tells us the line type:
        #   grp_xxx/account_number  → individual account line  → $50k/10% threshold
        #   grp_xxx (no suffix)     → section/group total line → $500k/10% threshold
        is_account_line = "/" in key_id
        t = thresh["mom_account"] if is_account_line else thresh["mom_total"]
    else:
        t = thresh["default"]

    return (abs(entry['var_dollar']) >= t['dollar'] and
            abs(entry['var_pct'])    >= t['pct'])


def load_numeric_cache():
    """Load cached Numeric task data, or return None if not present."""
    if NUMERIC_CACHE_FILE.exists():
        try:
            with open(NUMERIC_CACHE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return None


def save_numeric_cache(data):
    """Save Numeric task data to cache file."""
    with open(NUMERIC_CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def parse_review_notes_payload(payload, team_lookup):
    """Convert raw review notes payload into dashboard items.

    payload is a list of:
      {
        "task_name": str,
        "task_url":  str,
        "task_team": str,          # optional
        "review_notes": [
          {
            "assignee_name": str,  # or "assignee" dict with "name" key
            "status": str,         # "unresolved" / "UNRESOLVED" / etc.
            "body": str,           # or "content" / "text"
            ...
          }
        ]
      }

    team_lookup maps person name → team (built from existing task items).
    Returns a list of dashboard items (one per unresolved review note).
    """
    items = []
    for entry in payload:
        task_name = entry.get("task_name", "")
        task_url  = entry.get("task_url", "")
        task_team = entry.get("task_team", "")

        for note in entry.get("review_notes", []):
            # Status check — include only unresolved notes
            status_raw = (
                note.get("status") or note.get("state") or ""
            ).lower()
            if status_raw not in ("unresolved", "open", "pending", ""):
                continue  # resolved — skip

            # Assignee
            assignee = note.get("assignee_name") or ""
            if not assignee:
                a = note.get("assignee") or {}
                if isinstance(a, dict):
                    assignee = a.get("name") or a.get("full_name") or ""
                elif isinstance(a, str):
                    assignee = a
            if not assignee:
                continue  # no assignee — nothing to track

            # Note text for display
            body = (
                note.get("body") or note.get("content") or note.get("text") or ""
            ).strip()
            display_name = f"Review note on: {task_name}"
            if body:
                display_name += f" — {body[:80]}"

            # Team inference
            team = task_team or team_lookup.get(assignee, "Unknown Team")

            items.append({
                "team":        team,
                "person":      assignee,
                "type":        "review_note",
                "role":        "Assignee",
                "status":      "Unresolved",
                "name":        display_name,
                "report_name": "",
                "source":      "Numeric",
                "sheet":       "",
                "url":         task_url,
            })
    return items


def parse_numeric_tsv(tsv_text):
    """Parse Numeric list_tasks TSV into dashboard items.

    Determines which queue each task belongs to:
      prep_status=PENDING                          → Preparer queue
      prep_status=COMPLETE + review_status=PENDING → Reviewer queue
      review_status=COMPLETE (second review TBD)   → skipped (no assignee available)

    Team inference: if a task has no team tag, fall back to the person's
    most frequently assigned team across all their other tasks.
    """
    lines = tsv_text.strip().split("\n")
    if len(lines) < 2:
        return []

    # Skip any leading non-TSV summary lines (e.g. "4960 tasks") before the header
    start = 0
    for i, line in enumerate(lines):
        if "\t" in line:
            start = i
            break

    lines = lines[start:]
    if len(lines) < 2:
        return []

    headers = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        rows.append(dict(zip(headers, line.split("\t"))))

    # Pass 1: build person → most-frequent-team mapping from tasks that have a team
    from collections import Counter
    person_team_counts = defaultdict(Counter)
    for row in rows:
        team = row.get("Team", "").strip()
        if not team:
            continue
        for role_col in ("prep_assignee", "review_assignee"):
            person = row.get(role_col, "").strip()
            if person:
                person_team_counts[person][team] += 1

    def infer_team(person, explicit_team):
        if explicit_team:
            return explicit_team
        counts = person_team_counts.get(person)
        if counts:
            return counts.most_common(1)[0][0]
        return "Unknown Team"

    # Pass 2: build items
    variance_map  = load_variance_map()
    report_config = load_report_config()
    items = []
    for row in rows:
        task_type = NUMERIC_TYPE_MAP.get(row.get("task_type", ""), None)
        if not task_type:
            continue

        prep_assignee   = row.get("prep_assignee", "").strip()
        review_assignee = row.get("review_assignee", "").strip()
        prep_status     = row.get("prep_status", "").strip()
        review_status   = row.get("review_status", "").strip()

        # Materiality filter: only skip flux tasks that haven't been started yet.
        # If prep is already COMPLETE, the explanation was written — the reviewer
        # must still sign off regardless of variance size.
        if task_type == "flux" and prep_status == "PENDING" and not is_flux_required(
            row.get("name", ""),
            row.get("report_id", ""),
            row.get("key_id", ""),
            variance_map,
            report_config,
        ):
            continue
        explicit_team   = row.get("Team", "").strip()
        name            = row.get("name", "").strip()
        url             = row.get("url", "").strip()

        # Determine active role & person
        if prep_status == "PENDING" and prep_assignee:
            person, role, status = prep_assignee, "Preparer", "Assigned"
        elif prep_status == "COMPLETE" and review_status == "PENDING" and review_assignee:
            person, role, status = review_assignee, "Reviewer", "Prepared"
        else:
            continue  # second review or complete — skip

        team = infer_team(person, explicit_team)

        items.append({
            "team": team,
            "person": person,
            "type": task_type,
            "role": role,
            "status": status,
            "name": name,
            "report_name": "",
            "source": "Numeric",
            "sheet": "",
            "url": url,
        })

    return items


# Case-insensitive status matching (prefixes to catch "Reviewed (1/2)" etc.)
OUTSTANDING_STATUS_PREFIXES = ("assigned", "prepared", "reviewed", "unresolved")
BLANK_VALUES = {"unassigned", "uncategorized", "", "none", "n/a", "na", "-"}

# Column name variations (lowercase for matching)
COLUMN_ALIASES = {
    "preparer": ["preparer", "prepared by", "preparer name", "assigned to", "owner"],
    "reviewer": ["reviewer", "reviewed by", "reviewer name", "reviewer 1", "approver"],
    "reviewer_2": ["reviewer 2", "reviewer2", "second reviewer", "reviewer two", "approver 2"],
    "team": ["team", "team name", "department", "group", "dept"],
    "status": ["status", "state", "workflow status", "task status", "current status"],
    "name": ["name", "task", "item", "description", "report", "report name", "task name",
             "item name", "title", "flux name", "reconciliation", "checklist item",
             "group", "account"],
}


def find_column(df, column_type):
    """Find a column by checking aliases (case-insensitive)."""
    aliases = COLUMN_ALIASES.get(column_type, [column_type])
    df_cols_lower = {col.lower().strip(): col for col in df.columns}

    for alias in aliases:
        if alias in df_cols_lower:
            return df_cols_lower[alias]

    # Partial match fallback
    for alias in aliases:
        for col_lower, col_orig in df_cols_lower.items():
            if alias in col_lower or col_lower in alias:
                return col_orig

    return None


def clean_value(val):
    """Treat Unassigned/Uncategorized/etc as blank."""
    if pd.isna(val):
        return ""
    val = str(val).strip()
    return "" if val.lower() in BLANK_VALUES else val


def normalize_status(status):
    """Normalize status for comparison."""
    if not status:
        return ""
    return status.lower().strip()


def is_outstanding_status(status):
    """Check if status is outstanding (case-insensitive, prefix match)."""
    normalized = normalize_status(status)
    return any(normalized.startswith(prefix) for prefix in OUTSTANDING_STATUS_PREFIXES)


def strip_parenthetical_suffix(name):
    """Remove trailing parenthetical like (3) from report names."""
    if pd.isna(name):
        return ""
    return re.sub(r"\s*\(\d+\)\s*$", "", str(name).strip())


def get_close_month():
    """Extract close month from checklist filename."""
    patterns = [
        r"Numeric\s+(\w+\s+\d{4})\s+Checklist",
        r"(\w+\s+\d{4})\s+Checklist",
        r"Checklist\s+(\w+\s+\d{4})",
        r"(\w+\s+\d{4})",
    ]

    for f in DATA_DIR.glob("*.xlsx"):
        for pattern in patterns:
            match = re.search(pattern, f.name, re.IGNORECASE)
            if match:
                return match.group(1)

    for f in DATA_DIR.glob("*.xls"):
        for pattern in patterns:
            match = re.search(pattern, f.name, re.IGNORECASE)
            if match:
                return match.group(1)

    return "Unknown Period"


def classify_file(filename):
    """Classify file type from filename."""
    name_lower = filename.lower()
    name_upper = filename.upper()

    # Flux: starts with P&L or BS, or contains "flux"
    if name_upper.startswith("P&L") or name_upper.startswith("BS") or "flux" in name_lower:
        return "flux"
    elif "reconciliation" in name_lower or "recon" in name_lower:
        return "reconciliation"
    elif "checklist" in name_lower:
        return "checklist"
    elif "export" in name_lower or "review" in name_lower or "note" in name_lower:
        return "review_note"

    return None


def load_excel_files():
    """Load all Excel files from data directory."""
    files = {"flux": [], "reconciliation": [], "checklist": [], "review_note": []}
    errors = []
    loaded = []

    # Support both .xlsx and .xls
    excel_files = list(DATA_DIR.glob("*.xlsx")) + list(DATA_DIR.glob("*.xls"))

    for f in excel_files:
        # Skip temp files
        if f.name.startswith("~$"):
            continue

        file_type = classify_file(f.name)
        if not file_type:
            errors.append({"file": f.name, "error": "Could not determine file type from name"})
            continue

        try:
            # Try reading all sheets
            excel_file = pd.ExcelFile(f)
            sheets_loaded = 0

            for sheet_name in excel_file.sheet_names:
                try:
                    # Find the real header row by scanning for known column keywords
                    raw = pd.read_excel(excel_file, sheet_name=sheet_name, header=None)
                    header_row = 0
                    keywords = {"status", "preparer", "reviewer", "team", "name", "description"}
                    for i, row in raw.iterrows():
                        vals = {str(v).lower().strip() for v in row if str(v) != "nan"}
                        if len(vals & keywords) >= 2:
                            header_row = i
                            break

                    df = pd.read_excel(excel_file, sheet_name=sheet_name, header=header_row)

                    # Skip empty sheets
                    if df.empty or len(df.columns) < 2:
                        continue

                    # Skip sheets that look like metadata
                    if df.shape[0] < 1:
                        continue

                    df["_source_file"] = f.name
                    df["_source_sheet"] = sheet_name

                    # Clean column names
                    df.columns = [str(col).strip() for col in df.columns]

                    files[file_type].append(df)
                    sheets_loaded += 1

                except Exception as e:
                    errors.append({"file": f.name, "sheet": sheet_name, "error": str(e)})

            if sheets_loaded > 0:
                loaded.append({"file": f.name, "type": file_type, "sheets": sheets_loaded})

        except Exception as e:
            errors.append({"file": f.name, "error": str(e)})

    return files, loaded, errors


def email_to_name(email):
    """Convert email like 'rachel.walsh@gusto.com' to 'Rachel Walsh'."""
    if "@" not in email:
        return None
    local = email.split("@")[0]
    parts = re.split(r"[._]", local)
    return " ".join(p.capitalize() for p in parts if p)


def infer_person_teams(files):
    """Infer each person's team from their most common team across all files."""
    person_team_counts = defaultdict(lambda: defaultdict(int))

    for file_type, dfs in files.items():
        for df in dfs:
            team_col = find_column(df, "team")
            if not team_col:
                continue

            for role_type in ["preparer", "reviewer", "reviewer_2"]:
                role_col = find_column(df, role_type)
                if not role_col:
                    continue

                for _, row in df.iterrows():
                    person = clean_value(row.get(role_col, ""))
                    team = clean_value(row.get(team_col, ""))
                    if person and team:
                        person_team_counts[person][team] += 1

    person_teams = {}
    for person, teams in person_team_counts.items():
        if teams:
            person_teams[person] = max(teams, key=teams.get)

    # Build email-to-team mapping based on name matches
    name_to_team = {name.lower(): team for name, team in person_teams.items()}
    email_teams = {}
    for name, team in person_teams.items():
        # Create potential email patterns for this name
        parts = name.lower().split()
        if len(parts) >= 2:
            email_teams[f"{parts[0]}.{parts[-1]}"] = team
            email_teams[f"{parts[0]}_{parts[-1]}"] = team

    return person_teams, email_teams


def is_above_flux_threshold(row, df):
    """Return True if a flux row exceeds variance thresholds and should be flagged.

    Total lines  : flag only if |variance $| >= $500k AND |variance %| >= 10%
    Regular lines: flag only if |variance $| >= $50k  AND |variance %| >= 10%
    """
    var_dollar_col = next((c for c in df.columns if c.strip().lower() == "variance ($)"), None)
    var_pct_col    = next((c for c in df.columns if c.strip().lower() == "variance (%)"), None)

    # If columns are missing, include the item by default
    if var_dollar_col is None or var_pct_col is None:
        return True

    try:
        var_dollar = abs(float(row.get(var_dollar_col, 0) or 0))
    except (TypeError, ValueError):
        return True

    try:
        var_pct = abs(float(row.get(var_pct_col, 0) or 0))
    except (TypeError, ValueError):
        return True

    # Detect total lines by name starting with "total"
    name_col = find_column(df, "name")
    is_total = False
    if name_col:
        is_total = str(row.get(name_col, "")).strip().lower().startswith("total")

    if is_total:
        return var_dollar >= 500_000 and var_pct >= 0.10
    else:
        return var_dollar >= 50_000 and var_pct >= 0.10


def extract_outstanding_items(files, person_teams, email_teams, custom_mappings=None):
    """Extract all outstanding items grouped by team > person > type > role."""
    items = []
    role_mapping = {
        "preparer": "Preparer",
        "reviewer": "Reviewer",
        "reviewer_2": "Reviewer 2"
    }

    custom_mappings = custom_mappings or {"names": {}, "teams": {}}
    custom_names = custom_mappings.get("names", {})
    custom_teams = custom_mappings.get("teams", {})

    # Build reverse lookup: email local part -> display name
    email_to_display_name = {}
    for name in person_teams.keys():
        parts = name.lower().split()
        if len(parts) >= 2:
            email_to_display_name[f"{parts[0]}.{parts[-1]}"] = name
            email_to_display_name[f"{parts[0]}_{parts[-1]}"] = name

    def normalize_person(person):
        """Convert email to display name if possible, using custom mappings first."""
        # Check custom name mappings first (case-insensitive key lookup)
        person_lower = person.lower()
        for key, display_name in custom_names.items():
            if key.lower() == person_lower:
                return display_name

        if "@" in person:
            local = person.split("@")[0].lower()
            # Check if the email local part is in custom mappings
            for key, display_name in custom_names.items():
                if key.lower() == local or key.lower() == person_lower:
                    return display_name
            if local in email_to_display_name:
                return email_to_display_name[local]
        return person

    def get_team_for_person(person):
        """Look up team by name or email, using custom mappings first."""
        normalized = normalize_person(person)

        # Check custom team mappings first (by normalized name)
        normalized_lower = normalized.lower()
        for key, team in custom_teams.items():
            if key.lower() == normalized_lower:
                return team

        # Also check by original person string (for emails)
        person_lower = person.lower()
        for key, team in custom_teams.items():
            if key.lower() == person_lower:
                return team

        if normalized in person_teams:
            return person_teams[normalized]
        # Try email matching for unknown emails
        if "@" in person:
            local = person.split("@")[0].lower()
            if local in email_teams:
                return email_teams[local]
        return "Unknown Team"

    for file_type, dfs in files.items():
        for df in dfs:
            status_col = find_column(df, "status")
            if not status_col:
                continue

            name_col = find_column(df, "name")

            for _, row in df.iterrows():
                status = clean_value(row.get(status_col, ""))
                if not is_outstanding_status(status):
                    continue

                # Capitalize first letter of status for display
                display_status = status.capitalize() if status else ""
                status_lower = status.lower()

                # Determine which role's queue this item is in based on status
                # Assigned → Preparer, Prepared → Reviewer, Reviewed/Reviewed (1/2) → Reviewer 2
                # Unresolved → Preparer (for review notes)
                if status_lower.startswith("assigned"):
                    active_role = "preparer"
                elif status_lower.startswith("prepared"):
                    active_role = "reviewer"
                elif status_lower.startswith("reviewed"):
                    active_role = "reviewer_2"
                elif status_lower.startswith("unresolved"):
                    active_role = "preparer"
                else:
                    continue

                role_col = find_column(df, active_role)
                if not role_col:
                    continue

                # Skip flux items that are under variance thresholds
                if file_type == "flux" and not is_above_flux_threshold(row, df):
                    continue

                person_raw = clean_value(row.get(role_col, ""))
                if not person_raw:
                    continue

                person = normalize_person(person_raw)
                team = get_team_for_person(person_raw)

                item_name = ""
                if name_col:
                    item_name = clean_value(row.get(name_col, ""))

                # For flux, use the filename as the report name (e.g., "BS GAAP MoM")
                report_name = ""
                if file_type == "flux":
                    source_file = row.get("_source_file", "")
                    # Remove extension and parenthetical suffix
                    report_name = source_file.replace(".xlsx", "").replace(".xls", "")
                    report_name = strip_parenthetical_suffix(report_name)

                items.append({
                    "team": team,
                    "person": person,
                    "type": file_type,
                    "role": role_mapping[active_role],
                    "status": display_status,
                    "name": item_name,
                    "report_name": report_name,
                    "source": row.get("_source_file", ""),
                    "sheet": row.get("_source_sheet", "")
                })

    return items


def build_dashboard_data(items):
    """Build hierarchical data structure for dashboard."""
    dashboard = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(list))))

    for item in items:
        dashboard[item["team"]][item["person"]][item["type"]][item["role"]].append(item)

    # Convert to regular dict for JSON serialization
    result = {}
    for team, team_data in dashboard.items():
        result[team] = {}
        for person, person_data in team_data.items():
            result[team][person] = {}
            for item_type, type_data in person_data.items():
                result[team][person][item_type] = dict(type_data)

    return result


def build_detailed_message(items, close_month):
    """Build detailed Slack message: team > report type > person with counts and roles."""
    if not items:
        return f":white_check_mark: Month-End Close Status: {close_month}\n\nNo outstanding items!"

    lines = [f":calendar: Month-End Close Status: {close_month}"]
    lines.append(f":warning: {len(items)} outstanding items\n")

    by_team = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(int))))

    for item in items:
        by_team[item["team"]][item["type"]][item["person"]][item["role"]] += 1

    type_order = ["flux", "reconciliation", "checklist", "review_note"]
    type_labels = {
        "flux": ":chart_with_upwards_trend: Flux",
        "reconciliation": ":scales: Reconciliation",
        "checklist": ":ballot_box_with_check: Checklist",
        "review_note": ":memo: Review Note"
    }

    for team in sorted(by_team.keys()):
        lines.append(f"\n{team}")
        for report_type in type_order:
            if report_type not in by_team[team]:
                continue
            lines.append(f"  {type_labels.get(report_type, report_type)}")
            for person in sorted(by_team[team][report_type].keys()):
                roles = by_team[team][report_type][person]
                role_parts = [f"{role}: {count}" for role, count in sorted(roles.items())]
                lines.append(f"    {person}: {', '.join(role_parts)}")

    return "\n".join(lines)


def build_summary_message(items, close_month):
    """Build summary Slack message: team > simple bullet list of count + type + role."""
    if not items:
        return f":white_check_mark: Month-End Close Summary: {close_month}\n\nNo outstanding items!"

    lines = [f":calendar: Month-End Close Summary: {close_month}"]
    lines.append(f":warning: {len(items)} outstanding items\n")

    # For non-flux: group by team > type > role
    # For flux: group by team > report_name > role
    by_team_type_role = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    flux_by_team_report_role = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))

    for item in items:
        if item["type"] == "flux":
            report_label = item.get("report_name") or "Flux"
            flux_by_team_report_role[item["team"]][report_label][item["role"]] += 1
        else:
            by_team_type_role[item["team"]][item["type"]][item["role"]] += 1

    type_labels = {
        "reconciliation": ("Reconciliation", "Reconciliations"),
        "checklist":      ("Checklist",      "Checklists"),
        "review_note":    ("Review Note",    "Review Notes"),
    }
    type_order = ["checklist", "reconciliation", "review_note"]
    role_order = ["Preparer", "Reviewer", "Reviewer 2"]

    all_teams = sorted(set(by_team_type_role.keys()) | set(flux_by_team_report_role.keys()))

    for team in all_teams:
        lines.append(f"\n{team}")

        # Non-flux items (checklist, reconciliation, review notes)
        for item_type in type_order:
            if item_type in by_team_type_role[team]:
                for role in role_order:
                    count = by_team_type_role[team][item_type].get(role, 0)
                    if count > 0:
                        singular, plural = type_labels.get(item_type, (item_type, item_type))
                        label = singular if count == 1 else plural
                        lines.append(f"  • {count} {label} — {role}")

        # Flux items by report name (or generic "Flux" if no report name)
        if team in flux_by_team_report_role:
            for report_name in sorted(flux_by_team_report_role[team].keys()):
                for role in role_order:
                    count = flux_by_team_report_role[team][report_name].get(role, 0)
                    if count > 0:
                        lines.append(f"  • {count} {report_name} — {role}")

    return "\n".join(lines)


def send_slack_message(channel, message):
    """Send message to Slack channel via webhook."""
    # Use webhook URLs from .env
    if channel == "#pe-close-monitoring":
        webhook_url = os.getenv("SLACK_WEBHOOK_DETAILED")
    else:
        webhook_url = os.getenv("SLACK_WEBHOOK_SUMMARY")

    if not webhook_url:
        return {"ok": False, "error": f"Webhook URL not configured. Add SLACK_WEBHOOK_DETAILED and SLACK_WEBHOOK_SUMMARY to .env file"}

    try:
        response = requests.post(
            webhook_url,
            headers={"Content-Type": "application/json"},
            json={"text": message, "mrkdwn": True},
            timeout=10
        )

        # Webhooks return "ok" as plain text on success
        if response.status_code == 200 and response.text == "ok":
            return {"ok": True}
        else:
            return {"ok": False, "error": f"Slack returned: {response.text}"}

    except requests.exceptions.Timeout:
        return {"ok": False, "error": "Request to Slack timed out"}
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": f"Network error: {str(e)}"}


def send_via_slack_app(channel, message):
    """Send message to Slack channel via the desktop app using AppleScript."""
    # Remove the # from channel name if present
    channel_name = channel.lstrip("#")

    # Copy message to clipboard
    subprocess.run(["pbcopy"], input=message.encode("utf-8"), check=True)

    applescript = f'''
    tell application "Slack"
        activate
        delay 1
    end tell

    tell application "System Events"
        tell process "Slack"
            keystroke "k" using command down
            delay 0.5
            keystroke "{channel_name}"
            delay 0.5
            keystroke return
            delay 0.5
            keystroke "v" using command down
            delay 0.3
            keystroke return
        end tell
    end tell
    '''

    try:
        subprocess.run(["osascript", "-e", applescript], check=True, capture_output=True)
        return {"ok": True}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": f"AppleScript failed: {e.stderr.decode()}"}


def get_items_and_meta():
    """Return (items, close_month, source_info) from Numeric cache if available, else Excel files."""
    cache = load_numeric_cache()
    if cache:
        items = list(cache["items"])
        # Merge unresolved review notes stored in the same cache
        review_note_items = cache.get("review_note_items", [])
        items.extend(review_note_items)
        return (
            items,
            cache.get("close_month", "Unknown Period"),
            {"source": "numeric", "synced_at": cache.get("synced_at"), "errors": []},
        )
    # Fallback: Excel files
    files, loaded, errors = load_excel_files()
    person_teams, email_teams = infer_person_teams(files)
    custom_mappings = load_mappings()
    items = extract_outstanding_items(files, person_teams, email_teams, custom_mappings)
    close_month = get_close_month()
    return items, close_month, {"source": "excel", "files_loaded": loaded, "errors": errors}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """Receive task data from Numeric (via Claude) and cache it."""
    try:
        payload = request.get_json()
        tsv_text = payload.get("tsv", "")
        close_month = payload.get("close_month", "Unknown Period")

        # Completeness: count raw TSV rows before filtering
        tsv_lines = [l for l in tsv_text.strip().split("\n") if l.strip()]
        raw_row_count = max(0, len(tsv_lines) - 1)  # subtract header

        items = parse_numeric_tsv(tsv_text)
        from datetime import datetime, timezone
        synced_at = datetime.now(timezone.utc).isoformat()

        save_numeric_cache({
            "close_month": close_month,
            "synced_at": synced_at,
            "items": items,
        })

        # Write sync log entry for completeness evidence
        log_entry = {
            "synced_at": synced_at,
            "close_month": close_month,
            "raw_rows_received": raw_row_count,
            "items_after_filtering": len(items),
            "filtered_out": raw_row_count - len(items),
        }
        try:
            existing = []
            if SYNC_LOG_FILE.exists():
                with open(SYNC_LOG_FILE) as f:
                    existing = json.load(f)
            existing.append(log_entry)
            with open(SYNC_LOG_FILE, "w") as f:
                json.dump(existing[-50:], f, indent=2)  # keep last 50 entries
        except Exception:
            pass  # log failure is non-fatal

        return jsonify({
            "ok": True,
            "items_loaded": len(items),
            "close_month": close_month,
            "raw_rows_received": raw_row_count,
            "filtered_out": raw_row_count - len(items),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/sync_log", methods=["GET"])
def api_sync_log():
    """Return sync history for completeness review."""
    try:
        if SYNC_LOG_FILE.exists():
            with open(SYNC_LOG_FILE) as f:
                return jsonify(json.load(f))
        return jsonify([])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync_review_notes", methods=["POST"])
def api_sync_review_notes():
    """Receive unresolved review note data from Numeric (via Claude) and merge into cache.

    Payload: { "review_notes": [ { "task_name", "task_url", "task_team", "review_notes": [...] } ] }
    """
    try:
        payload = request.get_json()
        review_notes_payload = payload.get("review_notes", [])

        # Load existing cache so we can build team lookup and merge
        cache = load_numeric_cache()
        if not cache:
            return jsonify({"ok": False, "error": "No task cache found — run sync first"}), 400

        # Build person → team lookup from existing task items
        team_lookup = {}
        for item in cache.get("items", []):
            person = item.get("person", "")
            team   = item.get("team", "")
            if person and team and team != "Unknown Team":
                team_lookup.setdefault(person, team)

        review_note_items = parse_review_notes_payload(review_notes_payload, team_lookup)

        # Save back into cache
        cache["review_note_items"] = review_note_items
        save_numeric_cache(cache)

        return jsonify({
            "ok": True,
            "review_notes_loaded": len(review_note_items),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/dashboard")
def api_dashboard():
    items, close_month, meta = get_items_and_meta()
    dashboard = build_dashboard_data(items)

    return jsonify({
        "close_month": close_month,
        "dashboard": dashboard,
        "total_items": len(items),
        "data_source": meta.get("source"),
        "synced_at": meta.get("synced_at"),
        "files_loaded": meta.get("files_loaded", []),
        "errors": meta.get("errors", []),
        "has_slack_webhook": bool(os.getenv("SLACK_WEBHOOK_DETAILED") and os.getenv("SLACK_WEBHOOK_SUMMARY"))
    })


@app.route("/api/mappings")
def api_get_mappings():
    """Get current custom mappings."""
    return jsonify(load_mappings())


@app.route("/api/mappings", methods=["POST"])
def api_save_mappings():
    """Save custom mappings."""
    try:
        mappings = request.get_json()
        if not isinstance(mappings, dict):
            return jsonify({"ok": False, "error": "Invalid mappings format"}), 400
        # Ensure structure
        mappings.setdefault("names", {})
        mappings.setdefault("teams", {})
        save_mappings(mappings)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/unmapped")
def api_get_unmapped():
    """Get list of people who need name/team mappings."""
    items, _, _ = get_items_and_meta()

    # Find unique people who are emails or in Unknown Team
    unmapped_names = set()  # People displayed as emails
    unmapped_teams = set()  # People in Unknown Team

    for item in items:
        person = item["person"]
        team = item["team"]

        # Check if person looks like an email (contains @)
        if "@" in person:
            unmapped_names.add(person)

        # Check if team is unknown
        if team == "Unknown Team":
            unmapped_teams.add(person)

    return jsonify({
        "unmapped_names": sorted(list(unmapped_names)),
        "unmapped_teams": sorted(list(unmapped_teams)),
        "current_mappings": load_mappings()
    })


@app.route("/api/preview-detailed")
def preview_detailed():
    items, close_month, _ = get_items_and_meta()
    message = build_detailed_message(items, close_month)
    return jsonify({"message": message, "channel": "#pe-close-monitoring"})


@app.route("/api/preview-summary")
def preview_summary():
    items, close_month, _ = get_items_and_meta()
    message = build_summary_message(items, close_month)
    return jsonify({"message": message, "channel": "#accounting_close"})


@app.route("/api/send-detailed", methods=["POST"])
def send_detailed():
    items, close_month, _ = get_items_and_meta()
    message = build_detailed_message(items, close_month)
    result = send_via_slack_app("#pe-close-monitoring", message)
    return jsonify(result)


@app.route("/api/send-summary", methods=["POST"])
def send_summary():
    items, close_month, _ = get_items_and_meta()
    message = build_summary_message(items, close_month)
    result = send_via_slack_app("#accounting_close", message)
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True, port=5050)
