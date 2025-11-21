import os
import json
from flask import request, jsonify
from googleapiclient import discovery
from google.auth import default
from datetime import datetime, timezone, timedelta
from dateutil import parser

# ── Configuration ────────────────────────────────────────────────
ORG_ID = os.getenv("ORG_ID")  # Optional: restrict to a single organization ID
PROJECT_FILTER = os.getenv("PROJECT_FILTER")  # Optional: regex filter for project IDs
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "180"))
cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

# ── Authenticated client helper ─────────────────────────────────
def get_client(service_name, version):
    creds, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return discovery.build(service_name, version, credentials=creds, cache_discovery=False)

# ── List all active projects ────────────────────────────────────
def list_projects():
    crm = get_client("cloudresourcemanager", "v1")
    req = crm.projects().list()
    projects = []
    while req is not None:
        resp = req.execute()
        for p in resp.get("projects", []):
            if p.get("lifecycleState") != "ACTIVE":
                continue
            if ORG_ID:
                parent = p.get("parent", {})
                if str(parent.get("id")) != str(ORG_ID):
                    continue
            projects.append({"projectId": p["projectId"], "name": p.get("name")})
        req = crm.projects().list_next(previous_request=req, previous_response=resp)
    return projects

# ── List snapshots in a project ─────────────────────────────────
def list_snapshots(project_id):
    compute = get_client("compute", "v1")
    req = compute.snapshots().list(project=project_id)
    snapshots = []
    while req is not None:
        resp = req.execute()
        for snap in resp.get("items", []):
            snapshots.append(snap)
        req = compute.snapshots().list_next(previous_request=req, previous_response=resp)
    return snapshots

# ── Delete snapshots older than retention ───────────────────────
def delete_old_snapshots(project_id):
    compute = get_client("compute", "v1")

    deleted, failed, considered = 0, 0, 0
    for snap in list_snapshots(project_id):
        considered += 1
        created = parser.isoparse(snap["creationTimestamp"])
        if created < cutoff:
            try:
                compute.snapshots().delete(
                    project=project_id, snapshot=snap["name"]
                ).execute()
                deleted += 1
                print(f"Deleted snapshot {snap['name']} in {project_id}")
            except Exception as e:
                print(f"Failed to delete {snap['name']} in {project_id}: {e}")
                failed += 1
    return {"deleted": deleted, "failed": failed, "considered": considered}

# ── Main deletion runner ────────────────────────────────────────
def _run_deletion():
    summary = {
        "deleted_count": 0,
        "failed_count": 0,
        "total_considered": 0,
        "project_reports": {},
    }
    projects = list_projects()
    for proj in projects:
        pid = proj["projectId"]
        stats = delete_old_snapshots(pid)
        summary["deleted_count"] += stats["deleted"]
        summary["failed_count"] += stats["failed"]
        summary["total_considered"] += stats["considered"]
        summary["project_reports"][pid] = stats
    return summary

# ── Cloud Function HTTP Entrypoint ──────────────────────────────
def delete_old_snapshots_http(request):
    """HTTP Cloud Function entry point for snapshot deletion."""
    try:
        result = _run_deletion()
        return jsonify(result)
    except Exception as e:
        print(f"Error during snapshot deletion: {e}")
        return jsonify({"error": str(e)}), 500
