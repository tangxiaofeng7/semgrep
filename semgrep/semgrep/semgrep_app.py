# Handle communication of findings / errors to semgrep.app
import json
import os
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple

import click
import requests
from urllib3.util.retry import Retry

from semgrep.constants import SEMGREP_URL
from semgrep.constants import SEMGREP_USER_AGENT
from semgrep.error import SemgrepError
from semgrep.rule import Rule
from semgrep.rule_match import RuleMatchMap
from semgrep.util import partition
from semgrep.verbose_logging import getLogger

logger = getLogger(__name__)


# 4, 8, 16 seconds
RETRYING_ADAPTER = requests.adapters.HTTPAdapter(
    max_retries=Retry(
        total=3,
        backoff_factor=4,
        allowed_methods=["GET", "POST"],
        status_forcelist=(413, 429, 500, 502, 503),
    ),
)


class ScanHandler:
    def __init__(self, app_url: str, token: str) -> None:
        self.app_url = app_url
        session = requests.Session()
        session.mount("https://", RETRYING_ADAPTER)
        session.headers["User-Agent"] = SEMGREP_USER_AGENT
        session.headers["Authorization"] = f"Bearer {token}"
        self.session = session
        self.deployment_id, self.deployment_name = self._get_deployment_details()

        self.scan_id = None
        self.ignore_patterns: List[str] = []
        self._autofix = False

    @property
    def autofix(self) -> bool:
        """
        Seperate property for easy of mocking in test
        """
        return self._autofix

    def _get_deployment_details(self) -> Tuple[Optional[int], Optional[str]]:
        """
        Returns the deployment_id attached to an api_token as int

        Returns None if api_token is invalid/doesn't have associated deployment
        """
        r = self.session.get(
            f"{self.app_url}/api/agent/deployment",
            timeout=10,
        )
        if r.ok:
            data = r.json()
            return data.get("deployment", {}).get("id"), data.get("deployment", {}).get(
                "name"
            )
        else:
            return None, None

    def start_scan(self, meta: Dict[str, Any]) -> None:
        """
        Get scan id and file ignores

        returns ignored list
        """
        response = self.session.post(
            f"{self.app_url}/api/agent/deployment/{self.deployment_id}/scan",
            json={"meta": meta},
            timeout=30,
        )

        if response.status_code == 404:
            raise Exception(
                "Failed to create a scan with given token and deployment_id."
                "Please make sure they have been set correctly."
                f"API server at {self.app_url} returned this response: {response.text}"
            )

        try:
            response.raise_for_status()
        except requests.RequestException:
            raise Exception(
                f"API server at {self.app_url} returned this error: {response.text}"
            )

        body = response.json()
        self.scan_id = body["scan"]["id"]
        self._autofix = body.get("autofix", False)
        self.ignore_patterns = body["scan"]["meta"].get("ignored_files", [])

    @property
    def scan_rules_url(self) -> str:
        return f"{self.app_url}/api/agent/scan/{self.scan_id}/rules.yaml"

    def report_failure(self, exit_code: int) -> None:
        """
        Send semgrep cli non-zero exit code information to server
        and return what exit code semgrep should exit with.
        """
        response = self.session.post(
            f"{self.app_url}/api/agent/scan/{self.scan_id}/error",
            json={
                "exit_code": exit_code,
                "stderr": "",
            },
            timeout=30,
        )

        try:
            response.raise_for_status()
        except requests.RequestException:
            raise Exception(f"API server returned this error: {response.text}")

    def report_findings(
        self,
        matches_by_rule: RuleMatchMap,
        errors: List[SemgrepError],
        rules: List[Rule],
        targets: Set[Path],
        total_time: float,
        commit_date: str,
    ) -> None:
        """
        commit_date here for legacy reasons. epoch time of latest commit
        """
        all_ids = [r.id for r in rules]
        cai_ids, rule_ids = partition(
            lambda r_id: "r2c-internal-cai" in r_id,
            all_ids,
        )

        all_matches = [
            match
            for matches_of_rule in matches_by_rule.values()
            for match in matches_of_rule
        ]
        new_ignored, new_matches = partition(
            lambda match: match.is_ignored,
            all_matches,
        )

        findings = {
            # send a backup token in case the app is not available
            "token": os.getenv("GITHUB_TOKEN"),
            "gitlab_token": os.getenv("GITLAB_TOKEN"),
            "findings": [
                match.to_app_finding_format(commit_date) for match in new_matches
            ],
            "searched_paths": [str(t) for t in targets],
            "rule_ids": rule_ids,
            "cai_ids": cai_ids,
        }
        ignores = {
            "findings": [
                match.to_app_finding_format(commit_date) for match in new_ignored
            ],
        }
        complete = {
            "exit_code": 1
            if any(match.is_blocking and not match.is_ignored for match in all_matches)
            else 0,
            "stats": {
                "findings": len(new_matches),
                "errors": [error.to_dict() for error in errors],
                "total_time": total_time,
            },
        }

        logger.debug(f"Sending findings blob: {json.dumps(findings, indent=4)}")
        logger.debug(f"Sending ignores blob: {json.dumps(ignores, indent=4)}")
        logger.debug(f"Sending complete blob: {json.dumps(complete, indent=4)}")

        response = self.session.post(
            f"{SEMGREP_URL}/api/agent/scan/{self.scan_id}/findings",
            json=findings,
            timeout=30,
        )
        try:
            response.raise_for_status()

            resp_errors = response.json()["errors"]
            for error in resp_errors:
                message = error["message"]
                click.echo(f"Server returned following warning: {message}", err=True)

        except requests.RequestException:
            raise Exception(f"API server returned this error: {response.text}")

        response = self.session.post(
            f"{SEMGREP_URL}/api/agent/scan/{self.scan_id}/ignores",
            json=ignores,
            timeout=30,
        )
        try:
            response.raise_for_status()
        except requests.RequestException:
            raise Exception(f"API server returned this error: {response.text}")

        # mark as complete
        response = self.session.post(
            f"{SEMGREP_URL}/api/agent/scan/{self.scan_id}/complete",
            json=complete,
            timeout=30,
        )

        try:
            response.raise_for_status()
        except requests.RequestException:
            raise Exception(
                f"API server at {SEMGREP_URL} returned this error: {response.text}"
            )
