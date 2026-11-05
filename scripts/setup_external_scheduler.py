"""Provision two cron-job.org jobs without logging either credential.

Only use a dedicated fine-grained GitHub token scoped to this repository's
Actions write permission. Do not use a general Git credential or another key.
"""
import argparse
import json
import os
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def job_specs(repository, token):
    def make(title, workflow, hours, minutes, inputs):
        return {
            "title": title, "enabled": True, "saveResponses": False,
            "url": f"https://api.github.com/repos/{repository}/actions/workflows/{workflow}/dispatches",
            "requestMethod": 1,  # POST per cron-job.org's RequestMethod enum
            "requestTimeout": 30, "redirectSuccess": False,
            "schedule": {"timezone": "Asia/Shanghai", "expiresAt": 0,
                         "hours": hours, "minutes": minutes,
                         "mdays": [-1], "months": [-1], "wdays": [-1]},
            "extendedData": {"headers": {
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "TrendRadar-independent-scheduler",
            }, "body": json.dumps({"ref": "master", "inputs": inputs})},
        }

    return [
        make("TrendRadar news (08/11/17 Beijing)", "crawler.yml",
             [8, 9, 11, 12, 17, 18], [0, 10, 30, 50], {"send_now": False, "check_only": False}),
        make("TrendRadar check-in (07 Beijing)", "clean-crawler.yml", [7], [0, 20, 40], {}),
    ]


def request_json(url, token, method="GET", body=None):
    req = Request(url, method=method,
                  data=json.dumps(body).encode() if body is not None else None,
                  headers={"Authorization": "Bearer " + token,
                           "Content-Type": "application/json", "Accept": "application/json",
                           "User-Agent": "TrendRadar-scheduler-setup"})
    try:
        with urlopen(req, timeout=30) as response:
            payload = response.read()
            return json.loads(payload) if payload else {}
    except HTTPError as exc:
        # Never dump request headers or a server response that may echo them.
        raise RuntimeError(f"Scheduler API rejected the request (HTTP {exc.code})") from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    repository = os.environ.get("GITHUB_REPOSITORY", "nemowori/TrendRadar")
    if repository != "nemowori/TrendRadar":
        raise SystemExit("This setup is deliberately restricted to nemowori/TrendRadar")
    if args.plan:
        print(json.dumps(job_specs(repository, "<DEDICATED_REPOSITORY_ACTIONS_TOKEN>"), indent=2))
        return
    cron_key = os.environ.get("CRON_JOB_API_KEY", "").strip()
    github_key = os.environ.get("SCHEDULER_GITHUB_TOKEN", "").strip()
    if not cron_key or not github_key:
        raise SystemExit("Add CRON_JOB_API_KEY and SCHEDULER_GITHUB_TOKEN in GitHub Actions Secrets first")
    specs = job_specs(repository, github_key)
    # Validate GitHub authentication using a non-billable, no-message dispatch.
    request_json(specs[0]["url"], github_key, "POST",
                 {"ref": "master", "inputs": {"send_now": False, "check_only": True}})
    existing = request_json("https://api.cron-job.org/jobs", cron_key)
    if existing.get("someFailed"):
        raise RuntimeError("Incomplete scheduler job list; refusing to risk duplicate jobs")
    jobs = existing.get("jobs", [])
    for spec in specs:
        matches = [j for j in jobs if j["title"] == spec["title"] and j["url"] == spec["url"]]
        if len(matches) > 1:
            raise RuntimeError("Duplicate scheduler jobs already exist; inspect them before provisioning")
        if matches:
            job_id = matches[0]["jobId"]
            request_json(f"https://api.cron-job.org/jobs/{job_id}", cron_key, "PATCH", {"job": spec})
        else:
            result = request_json("https://api.cron-job.org/jobs", cron_key, "PUT", {"job": spec})
            job_id = result["jobId"]
        details = request_json(f"https://api.cron-job.org/jobs/{job_id}", cron_key)["jobDetails"]
        if not details["enabled"] or details["schedule"] != spec["schedule"]:
            raise RuntimeError("Scheduler did not retain the intended enabled schedule")
        print(json.dumps({"jobId": job_id, "title": spec["title"], "enabled": True,
                          "timezone": details["schedule"]["timezone"],
                          "nextExecution": details.get("nextExecution")}, ensure_ascii=False))
        time.sleep(1.1)  # Respect the create/update API rate limit.


if __name__ == "__main__":
    main()
