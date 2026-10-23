"""Bulk-set Veracode SCA agent-based project default branches. Uses unofficial UI endpoints."""

import argparse
import configparser
import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone

import requests
from veracode_api_py.apihelper import APIHelper
from veracode_api_py.sca import Workspaces

TIMEOUT = 30
LAST_SCANNED = "Use Last Scanned"
log = logging.getLogger("set-default-branch")


def load_profile(profile):
    path = os.path.expanduser("~/.veracode/credentials")
    cfg = configparser.ConfigParser()
    if not cfg.read(path):
        sys.exit(f"Credentials file not found: {path}")
    if not cfg.has_section(profile):
        sys.exit(f"Profile '{profile}' not found. Available: {', '.join(cfg.sections())}")
    os.environ["VERACODE_API_KEY_ID"] = cfg.get(profile, "veracode_api_key_id")
    os.environ["VERACODE_API_KEY_SECRET"] = cfg.get(profile, "veracode_api_key_secret")


class UiSession:
    def __init__(self, instance):
        self.base = f"https://ui.analysiscenter.veracode.{instance}/srcclr"
        principal = APIHelper()._rest_request("api/authn/v2/principal", "GET")
        token = principal.get("token")
        if not token:
            sys.exit("No token in principal response. Are these human-user API credentials?")
        self.s = requests.Session()
        self.s.cookies.set("vsid", token)
        r = self.s.get(f"{self.base}/user-status", timeout=TIMEOUT)
        r.raise_for_status()
        xsrf = self.s.cookies.get("XSRF-TOKEN")
        if not xsrf:
            sys.exit("XSRF-TOKEN cookie not returned by user-status")
        self.s.headers["X-XSRF-TOKEN"] = xsrf

    def repo_defaults(self, ws_site_id):
        payload = {
            "scope": {"agents": "*", "paths": "*", "repos": "*", "favoriteReposOnly": False,
                      "scanAfterDate": None, "scanBeforeDate": None},
            "filter": {"vulnsOnly": False, "vulnMethodsOnly": False, "search": ""},
        }
        r = self.s.post(f"{self.base}/teams/{ws_site_id}/reports/repos", json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        repos = r.json().get("_embedded", {}).get("repos", [])
        return {repo["id"]: repo.get("defaultBranch") for repo in repos if repo.get("id") is not None}

    def branches(self, repo_site_id):
        r = self.s.get(f"{self.base}/repos/{repo_site_id}/branches",
                       params={"searchTerm": "", "size": 9999}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json().get("content", [])

    def set_default(self, repo_site_id, branch):
        # branch=None switches the project back to Use Last Scanned.
        r = self.s.post(f"{self.base}/repos/{repo_site_id}/branches:default-branch",
                        json={"branch": branch, "useLatest": branch is None}, timeout=TIMEOUT)
        r.raise_for_status()


def branch_names(items):
    names = set()
    for b in items or []:
        if isinstance(b, str):
            names.add(b)
        elif isinstance(b, dict):
            for key in ("name", "branch", "ref"):
                if b.get(key):
                    names.add(b[key])
                    break
    return names


def load_csv(path):
    mapping = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            project, branch = (row.get("project") or "").strip(), (row.get("branch") or "").strip()
            if not project or not branch:
                continue
            if branch.lower() == LAST_SCANNED.lower():
                branch = LAST_SCANNED
            mapping[((row.get("workspace") or "").strip(), project)] = branch
    if not mapping:
        sys.exit(f"No usable rows in {path}. Expected header: workspace,project,branch")
    return mapping


def target_for(args, mapping, ws_name, proj_name):
    if mapping is None:
        if args.project and proj_name not in args.project:
            return None
        return LAST_SCANNED if args.reset else args.branch
    return mapping.get((ws_name, proj_name)) or mapping.get(("", proj_name))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Set Veracode SCA agent-based project default branches.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--branch", help="Branch to set as default on every selected project")
    src.add_argument("--reset", action="store_true", help="Set selected projects back to 'Use Last Scanned'")
    src.add_argument("--csv", help="Per-project targets: CSV with header workspace,project,branch")
    p.add_argument("--workspace", action="append", help="Only this workspace name (repeatable)")
    p.add_argument("--project", action="append", help="Only this project name (repeatable, not with --csv)")
    p.add_argument("--only-last-scanned", action="store_true",
                   help="Only change projects currently on 'Use Last Scanned'; keep explicit defaults")
    p.add_argument("--profile", default="default", help="~/.veracode/credentials profile (default: default)")
    p.add_argument("--instance", default="com", choices=["com", "eu", "us"], help="Platform region (default: com)")
    p.add_argument("--dry-run", action="store_true", help="Report what would change, change nothing")
    p.add_argument("--output", help="JSON report path (default: default-branch-report-<UTC>.json)")
    p.add_argument("--debug", action="store_true", help="Verbose logging")
    return p.parse_args(argv)


def run(args, ui, ws_api):
    mapping = load_csv(args.csv) if args.csv else None
    matched_rows = set()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {"timestamp": stamp, "dry_run": args.dry_run, "counts": {}, "projects": [], "unmatched_csv_rows": []}

    def record(ws, proj, action, msg, **extra):
        report["projects"].append({"workspace": ws.get("name"), "project": proj.get("name"),
                                   "site_id": proj.get("site_id"), "action": action, "message": msg, **extra})
        report["counts"][action] = report["counts"].get(action, 0) + 1
        log.info("[%s] %s / %s: %s", action, ws.get("name"), proj.get("name"), msg)

    for ws in ws_api.get_all():
        ws_name = ws.get("name")
        if args.workspace and ws_name not in args.workspace:
            continue
        try:
            defaults = ui.repo_defaults(ws.get("site_id"))
        except requests.RequestException as e:
            log.error("Workspace %s: cannot read default branches (%s), skipping", ws_name, e)
            continue

        for proj in ws_api.get_projects(ws["id"]):
            proj_name, sid = proj.get("name"), proj.get("site_id")
            target = target_for(args, mapping, ws_name, proj_name)
            if not target:
                continue
            if mapping is not None:
                matched_rows.add((ws_name, proj_name) if (ws_name, proj_name) in mapping else ("", proj_name))

            # Missing from the report means status unknown, not "Use Last Scanned".
            known = sid in defaults
            current = defaults.get(sid)
            current_label = current or (LAST_SCANNED if known else "unknown")
            reset = target == LAST_SCANNED
            target_label = LAST_SCANNED if reset else f"'{target}'"
            base = {"current_default": current_label, "target": target}

            if (reset and known and current is None) or (not reset and current == target):
                record(ws, proj, "no_action", f"Already {target_label}", **base)
                continue
            if args.only_last_scanned and (current or not known):
                why = "explicit default kept" if current else "not in workspace report, status unknown"
                record(ws, proj, "skipped", f"{why} (--only-last-scanned)", **base)
                continue

            if not reset:
                names = branch_names(proj.get("branches"))
                if target not in names:
                    try:
                        names = branch_names(ui.branches(sid))
                    except requests.RequestException as e:
                        record(ws, proj, "error", f"Branch lookup failed: {e}", **base)
                        continue
                if target not in names:
                    record(ws, proj, "skipped_no_branch", f"'{target}' never scanned for this project",
                           scanned=sorted(names), **base)
                    continue

            msg = f"{current_label} -> {target_label}"
            if args.dry_run:
                record(ws, proj, "would_update", msg, **base)
                continue
            try:
                ui.set_default(sid, None if reset else target)
                record(ws, proj, "updated", msg, **base)
            except requests.RequestException as e:
                record(ws, proj, "failed", f"{msg}: {e}", **base)

    if mapping is not None:
        report["unmatched_csv_rows"] = [{"workspace": w, "project": p} for (w, p) in mapping if (w, p) not in matched_rows]
        for row in report["unmatched_csv_rows"]:
            log.warning("CSV row matched no project: %s / %s", row["workspace"] or "*", row["project"])

    out_path = args.output or f"default-branch-report-{stamp}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    log.info("Summary: %s. Report: %s", report["counts"], out_path)
    return 1 if report["counts"].get("failed") or report["counts"].get("error") else 0


def main(argv=None):
    args = parse_args(argv)
    if args.project and args.csv:
        sys.exit("--project does not apply with --csv; put projects in the CSV instead")
    if args.reset and args.only_last_scanned:
        sys.exit("--only-last-scanned cannot be combined with --reset")
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    load_profile(args.profile)
    return run(args, UiSession(args.instance), Workspaces())


if __name__ == "__main__":
    sys.exit(main())
