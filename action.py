# -*- coding: utf-8 -*-
import logging
import re
import sys
import time
from pathlib import Path
from typing import Dict

import click

import requests
import tenacity

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class UnityCloudBuildClient:
    """
    Handles connectivity to Unity Cloud Build SaaS
    """

    def __init__(
        self,
        api_key: str,
        org_id: str,
        project_id: str,
        primary_build_target: str,
        target_platform: str,
        github_head_ref: str = "",
        download_binary: bool = False,
    ) -> None:
        """
        If this action is being kicked off via a pull request, then ``github_head_ref`` var will
        be set and this branch name will be used to generate a new unity build target to run the
        build against changes in that branch. If this var is not present, then the action falls back
        to the primary build target which is configured in the workflow itself and should point
        at the target at references the ``main`` branch on our repository.
        """
        logger.info("Setting up Unity Cloud Client...")
        self.api_base_url = "https://build-api.cloud.unity3d.com/api/v1"
        self.org_id = org_id.lower()
        self.project_id = project_id.lower()
        self.target_platform = target_platform.lower()
        self.api_key = api_key
        self.primary_build_target_id = primary_build_target.lower()
        self.pr_branch_name = github_head_ref
        self.pr_branch_name = self.pr_branch_name.replace("refs/heads/", "")
        self.download_binary = download_binary

    def prepare_headers(self) -> Dict:
        """
        prepares headers for the request - ensures we are authorized.
        """
        return {
            "Content-Type": "application/json",
            "Authorization": f"Basic {self.api_key}",
        }


    # List all the projects org. This is essentially to verify input settings
    def list_projects(self) -> Dict:
        logger.info(f"Fetching projects for {self.org_id}...")
        resp = requests.get(
            f"{self.api_base_url}/orgs/{self.org_id}/projects",
            headers=self.prepare_headers(),
            timeout=10
        )
        if resp.status_code == 200:
            Projects = ", ".join(x["projectid"] for x in resp.json())
            logger.info(f"Got organisation projects; {Projects}")
            return
        raise Exception(f"Failed to lookup projects for {self.org_id} - received status={resp.status_code} content={resp.text}")



    # List all the build targets for this org/project. This is essentially to verify credentials/org/project settings
    def list_build_targets(self) -> Dict:
        logger.info(f"Fetching build targets for {self.org_id}/{self.project_id}...")
        resp = requests.get(
            f"{self.api_base_url}/orgs/{self.org_id}/projects/{self.project_id}/buildtargets",
            headers=self.prepare_headers(),
            timeout=10
        )
        if resp.status_code == 200:
            BuildTargets = ", ".join(x["buildtargetid"] for x in resp.json())
            #logger.info(f"Got organisation projects; {Projects}")
            # todo: extract/format build targets from json
            logger.info(f"Got project build targets; {BuildTargets}")
            return resp.json()

        raise Exception(f"Failed to lookup build targets for {self.org_id}/{self.project_id} - received status={resp.status_code} content={resp.text}")



    def download_artifact(self, url: str) -> None:
        """ Downloads the final built binary to disk """
        logger.info("Downloading built binary to workspace...")
        resp = requests.get(url, allow_redirects=True)
        if resp.status_code == 200:
            try:
                if self.target_platform == "android":
                    filename = "android.aab"
                elif self.target_platform == "ios":
                    filename = "ios.ipa"
                elif self.target_platform == "webgl":
                    filename = "webgl.zip"
                p = Path("builds")
                p.mkdir(exist_ok=True)
                (p / filename).write_bytes(resp.content)
            except IOError:
                logger.critical("Could not write built binary to disk")
                sys.exit(1)
            logger.info("Download successful!")
            return
        logger.critical(
            f"Build could not be downloaded - received a HTTP {resp.status_code}"
        )
        sys.exit(1)

    
    def get_build_target(self, build_target_id: str) -> Dict:
        """
        Looks up a build target from Unity Cloud Build
        """
        logger.info(f"Fetching build target meta for {build_target_id}...")
        resp = requests.get(
            f"{self.api_base_url}/orgs/{self.org_id}/projects/{self.project_id}/buildtargets/{build_target_id}",
            headers=self.prepare_headers(),
            timeout=10
        )
        if resp.status_code == 200:
            return resp.json()
            
        raise Exception(f"Failed to lookup build target meta for {build_target_id} - received status={resp.status_code} content={resp.text}")


    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=4, max=10),
        stop=tenacity.stop_after_attempt(10),
        after=tenacity.after_log(logger, logging.DEBUG)
    )
    def set_build_target_env_var(
        self, build_target_id: str, key: str, value: str
    ) -> None:
        """
        sets an env var to a build target.

        TODO: If we need more than a couple of env vars- we should update this func to set vars in bulk
        """
        logger.info(f"Setting env var: {key} on target: {build_target_id}...")
        resp = requests.put(
            f"{self.api_base_url}/orgs/{self.org_id}/projects/{self.project_id}/buildtargets/{build_target_id}/envvars",
            headers=self.prepare_headers(),
            json={key: value},
        )
        if resp.status_code == 200:
            return
        raise Exception(f"Env var could not be set - received status={resp.status_code} content={resp.text}")





    #@tenacity.retry(
    #    wait=tenacity.wait_exponential(multiplier=1, min=4, max=10),
    #    stop=tenacity.stop_after_attempt(10),
    #    after=tenacity.after_log(logger, logging.DEBUG)
    #)
    def get_build_target_id(self) -> str:
        """
        Creates a new build target in Unity Cloud Build if we are dealing with a pull request.
        Otherwise we return the primary build target (main)
        """
        # get primary build target so that we can copy across all relevant settings to our PR target
        # always fetch it, to validate if the user-input target id is correct
        primary_build_target = self.get_build_target(self.primary_build_target_id)
        
        if self.pr_branch_name:

            # replace any special chars and ensure length is max of 56 chars
            # 64 is the limit, but we allow some free chars for platform
            branch_name = re.sub("[^0-9a-zA-Z]+", "-", self.pr_branch_name)[
                0:56
            ].lower()
            name = f"{self.target_platform}-{branch_name}"

            logger.info(
                f"Creating a build target for {self.project_id} for PR branch: {name}..."
            )

            # setup new payload copying relevant settings from the primary build target
            payload = {
                "name": name,
                "enabled": True,
                "platform": primary_build_target["platform"],
                "settings": primary_build_target["settings"],
            }

            # if building for ios or android, apply signing credentials
            if self.target_platform in ["android", "ios"]:
                creds = {
                    "credentials": {
                        "signing": {
                            "credentialid": primary_build_target["credentials"][
                                "signing"
                            ]["credentialid"]
                        }
                    }
                }
                payload.update(creds)

            # override payload settings with our PR branch name and remove any applied build schedules.
            payload["settings"]["scm"]["branch"] = self.pr_branch_name
            payload["settings"]["buildSchedule"] = {}

            # make the new build target
            resp = requests.post(
                f"{self.api_base_url}/orgs/{self.org_id}/projects/{self.project_id}/buildtargets",
                headers=self.prepare_headers(),
                json=payload,
                timeout=10
            )
            if resp.status_code == 201:
                data = resp.json()
                build_target_id = data["buildtargetid"]
                logger.info(f"Build target {build_target_id} created successfully!")
                return build_target_id
            # why unity thinks returning a http 500 for a validation error is ok, is beyond me. *facepalm*
            elif resp.status_code == 500:
                data = resp.json()
                error = data.get("error", "")
                if (
                    error
                    and error == "Build target name already in use for this project!"
                ):
                    logger.info(
                        f"Build target for this PR already exists: {name}. Re-using..."
                    )
                    return name
            raise Exception(f"Build target could not be created - received status={resp.status_code} content={resp.text}")

        # otherwise return the primary build target (main branch)
        return self.primary_build_target_id

    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=4, max=10),
        stop=tenacity.stop_after_attempt(10),
    )
    def start_build(self, build_target_id: str) -> int:
        """
        Kicks off a new build of the project for the correct build target
        """
        logger.info(
            f"Creating a build of {self.project_id} on target {build_target_id}..."
        )
        resp = requests.post(
            f"{self.api_base_url}/orgs/{self.org_id}/projects/{self.project_id}/buildtargets/{build_target_id}/builds",
            headers=self.prepare_headers(),
            json={"clean": False, "delay": 0},
        )
        if resp.status_code == 202:
            data = resp.json()
            build_number = data[0]["build"]
            logger.info(f"Build {build_number} created successfully!")
            return build_number
        raise Exception(
            f"Build could not be started - received a HTTP {resp.status_code}"
        )

    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=4, max=10),
        stop=tenacity.stop_after_attempt(10),
    )
    def get_build_status(self, build_target_id: str, build_number: int) -> None:
        """
        Gets the status of the running build

        Build status returned can be one of the below
        [queued, sentToBuilder, started, restarted, success, failure, canceled, unknown]

        We class "failure", "cancelled" and "unknown" as failures and return exit code 1.
        We class "success" as successful and return exit code 0
        All other statuses, we continue to poll.
        """
        logger.info(f"Checking status of build {build_number}...")
        failed_statuses = ["failure", "canceled", "cancelled", "unknown"]
        success_statuses = ["success"]
        resp = requests.get(
            f"{self.api_base_url}/orgs/{self.org_id}/projects/{self.project_id}/buildtargets/{build_target_id}/builds/{build_number}",
            headers=self.prepare_headers(),
        )
        if resp.status_code == 200:
            data = resp.json()
            status = data["buildStatus"]
            if status in failed_statuses:
                logger.critical(
                    f"Build {build_number} on project {self.project_id} on target {build_target_id} failed with status: {status}"
                )
                sys.exit(1)
            if status in success_statuses:
                logger.info(
                    f"Build {build_number} on project {self.project_id} on target {build_target_id} completed successfully!"
                )

                # if we have been asked to save the built binary to disk, then we download it.
                if self.download_binary:
                    self.download_artifact(data["links"]["download_primary"]["href"])
                sys.exit()
            logger.info(
                f"Build {build_number} on project {self.project_id} on target {build_target_id} is still running: {status}"
            )
            return
        raise Exception(
            f"Could not check status of build - received a HTTP {resp.status_code}"
        )


@click.command()
@click.argument("api_key", envvar="UNITY_CLOUD_BUILD_API_KEY", type=str)
@click.argument("org_id", envvar="UNITY_CLOUD_BUILD_ORG_ID", type=str)
@click.argument("project_id", envvar="UNITY_CLOUD_BUILD_PROJECT_ID", type=str)
@click.argument(
    "primary_build_target", envvar="UNITY_CLOUD_BUILD_PRIMARY_TARGET", type=str
)
@click.argument("target_platform", envvar="UNITY_CLOUD_BUILD_TARGET_PLATFORM", type=str)
@click.argument(
    "polling_interval",
    envvar="UNITY_CLOUD_BUILD_POLLING_INTERVAL",
    type=float,
    default=60.0,
)
@click.argument(
    "download_binary",
    envvar="UNITY_CLOUD_BUILD_DOWNLOAD_BINARY",
    type=bool,
    default=False,
)
@click.argument("github_head_ref", envvar="GITHUB_HEAD_REF", type=str, default="")
def main(
    api_key: str,
    org_id: str,
    project_id: str,
    primary_build_target: str,
    target_platform: str,
    polling_interval: float,
    download_binary: bool,
    github_head_ref: str,
) -> None:

    # validate incoming target platform
    target_platform = target_platform.lower()
    if target_platform not in ["android", "ios", "webgl"]:
        logger.critical("Target platform must be android, ios or webgl!")
        sys.exit(1)

    # create unity cloud build client
    client: UnityCloudBuildClient = UnityCloudBuildClient(
        api_key,
        org_id,
        project_id,
        primary_build_target,
        target_platform,
        github_head_ref,
        download_binary,
    )


    try:
        client.list_projects()
        client.list_build_targets()
    except BaseException as exception:
        logger.critical(f"Failed to get organisation projects, or project build targets. Credentials are probably incorrect; {exception}")
        sys.exit(1)
        
        
        
    # obtain the build target we need to run against
    try:
        build_target_id: str = client.get_build_target_id()
        logger.info(f"Acquired Build Target Id: {build_target_id}. Primary Target Id: {primary_build_target}")
    except BaseException as exception:
        logger.critical(f"Unable to obtain unity build target; {exception}")
        sys.exit(1)

    if build_target_id != primary_build_target:
        # set build platform env var for the PR build target
        try:
            client.set_build_target_env_var(
                build_target_id, "BUILD_PLATFORM", target_platform
            )
        except tenacity.RetryError:
            logger.critical(
                f"Unable to set env var BUILD_PLATFORM={target_platform} on {build_target_id} after 10 attempts!"
            )
            sys.exit(1)

    # create a new build for the specified build target
    try:
        build_number = client.start_build(build_target_id)
        logger.info(f"Started build number {build_number}")
    except tenacity.RetryError:
        logger.critical(
            f"Unable to start unity build {build_target_id} after 10 attempts!"
        )
        sys.exit(1)

    # poll the running build for updates waiting for an polling interval between each poll
    while True:
        try:
            client.get_build_status(build_target_id, build_number)
        except tenacity.RetryError:
            logger.critical(
                f"Unable to check status unity build {build_target_id} {build_number} after 10 attempts!"
            )
            sys.exit(1)
        logger.info(f"Waiting {polling_interval} seconds...")
        time.sleep(polling_interval)


if __name__ == "__main__":
    sys.exit(main())
