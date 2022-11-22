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

import os
import json


# general timeout for http requests
fetch_timeout_secs=60


# setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# make sure any files are written to the github workspace, in case the working directory is wrong
GITHUB_WORKSPACE = os.getenv('GITHUB_WORKSPACE')
# for local testing, could fall back to ./
if not GITHUB_WORKSPACE:
	raise Exception(f"GITHUB_WORKSPACE env variable is empty. Expecting this to be a directory")

# write out meta back to workflow via github output vars
github_output_filename = os.getenv('GITHUB_OUTPUT')
github_env_filename = os.getenv('GITHUB_ENV')

def write_github_output_and_env(key: str,value: str) -> None:
    
    if github_output_filename:
        with open(github_output_filename, "a") as output:
            output.write(f"{key}={value}\n")
            logger.info(f"Wrote GITHUB_OUTPUT var {key}={value}")
    
    if github_env_filename:
        with open(github_env_filename, "a") as env:
            env.write(f"{key}={value}\n")
            logger.info(f"Wrote GITHUB_ENV var {key}={value}")


# error script early if we can't write github env vars
write_github_output_and_env("github_output_test_key","test_value")

# hardcoded meta for platforms
# todo: remove these as they're not always correct.
#	infer filename from download url.
#	We already handle any filename in the output via ARTIFACT_FILEPATH
platform_default_artifact_filenames = {
  'ios':'Ios.ipa',
  'android':'Android.aab', # often is apk
  'webgl':'Webgl.zip',
  'windows':'Windows.zip',
  'mac':'Mac.app.zip',
}


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
        github_head_ref: str,
        allow_new_build_targets: bool,
    ) -> None:
        
        # The github_head_ref is now always required, and will be checked against the default target's configuration
        # new build targets will then be created, for pull requests, new branches, tags etc
        self.branch_name = github_head_ref
        self.branch_name = self.branch_name.replace("refs/heads/", "")
        if not self.branch_name:
            raise Exception($"No github_head_ref supplied, this is now required")
        self.allow_new_build_targets = allow_new_build_targets

        logger.info("Setting up Unity Cloud Client...")
        self.api_base_url = "https://build-api.cloud.unity3d.com/api/v1"
        self.org_id = org_id.lower()
        self.project_id = project_id.lower()
        self.target_platform = target_platform.lower()
        self.api_key = api_key
        self.primary_build_target_id = primary_build_target.lower()

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
            timeout=fetch_timeout_secs
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
            timeout=fetch_timeout_secs
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
        logger.info(f"Downloading built binary to workspace... {url}")
        resp = requests.get(url, allow_redirects=True)
        if resp.status_code == 200:
            try:
                # todo: dont rename files. eg. android output is .apk, use that
                filename = platform_default_artifact_filenames[self.target_platform]
                # files must be written inside the GITHUB_WORKSPACE
                filepath = Path( GITHUB_WORKSPACE ) / "artifacts" / filename
                
                if not filepath.is_relative_to(GITHUB_WORKSPACE):
                    logger.info(f"Warning: filepath({filepath.absolute()} is not relative to GITHUB_WORKSPACE({GITHUB_WORKSPACE})")

                directory = filepath.parent
                directory.mkdir(parents=True,exist_ok=True)
                filepath.write_bytes(resp.content)
            except IOError as exception:
                logger.critical(f"Could not write built binary to disk: {exception}")
                sys.exit(1)

            # need to output a github_workspace relative path
            workspacefilepath = str( filepath.relative_to(GITHUB_WORKSPACE) )
            meta = { "filename":filename, "filepath":workspacefilepath}
            logger.info(f"Download to {meta['filepath']} successful!")
            return meta
            
        logger.critical(f"Build could not be downloaded - http status={resp.status_code} content={resp.text}")
        # gr: throw instead of exiting here
        sys.exit(1)

    
    def get_build_target(self, build_target_id: str) -> Dict:
        """
        Looks up a build target from Unity Cloud Build
        """
        logger.info(f"Fetching build target meta for {build_target_id}...")
        resp = requests.get(
            f"{self.api_base_url}/orgs/{self.org_id}/projects/{self.project_id}/buildtargets/{build_target_id}",
            headers=self.prepare_headers(),
            timeout=fetch_timeout_secs
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
        primary_build_branch = primary_build_target["settings"]["scm"]["branch"]
        
        is_primary_build_target_branch_match = self.branch_name == primary_build_branch
        
        if not is_primary_build_target_branch_match:
            logger.info(f"Building branch {self.branch_name} doesn't match primary build target branch {primary_build_branch}, creating new branch")
            
            # todo: find existing build target with matching branch name
            # +? also match other configuration settings
            if not self.allow_new_build_targets:
                raise Exception(f"Creating new build targets not allowed")

            # replace any special chars and ensure length is max of 56 chars
            # 64 is the limit, but we allow some free chars for platform
            branch_name = re.sub("[^0-9a-zA-Z]+", "-", self.branch_name)[
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
            payload["settings"]["scm"]["branch"] = self.branch_name
            payload["settings"]["buildSchedule"] = {}

            # make the new build target
            resp = requests.post(
                f"{self.api_base_url}/orgs/{self.org_id}/projects/{self.project_id}/buildtargets",
                headers=self.prepare_headers(),
                json=payload,
                timeout=fetch_timeout_secs
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
            timeout=fetch_timeout_secs
        )
        if resp.status_code == 200:
            data = resp.json()
            status = data["buildStatus"]

            if status in failed_statuses:
                logger.critical(f"Build {build_number} on project {self.project_id} on target {build_target_id} failed with status: {status}")
                # gr: throw here instead of exiting?
                sys.exit(1)

            if status in success_statuses:
                logger.info(f"Build {build_number} on project {self.project_id} on target {build_target_id} completed successfully!")
                return data
                
            logger.info(f"Build {build_number} on project {self.project_id} on target {build_target_id} is still running: {status}")
            return
        raise Exception(f"Could not check status of build - http status={resp.status_code} content={resp.text}")

    def get_share_url_from_share_id(self, share_id:str ) -> str:
        return f"https://developer.cloud.unity3d.com/share/share.html?shareId={share_id}"

    def create_share_url(self, build_target_id: str,build_number: int) -> str:
        # if a share already exists for this build, it will be revoked and a new one created (note: same url as GET share meta)
        create_share_url = f"{self.api_base_url}/orgs/{self.org_id}/projects/{self.project_id}/buildtargets/{build_target_id}/builds/{build_number}/share"
        post_body = {'shareExpiry':''}
        response = requests.post(
                            create_share_url,
                            headers=self.prepare_headers(),
                            timeout=fetch_timeout_secs,
                            data=json.dumps(post_body)
        )
        if response.status_code != 200:
            raise Exception(f"Failed to get create share for build - http status={response.status_code} content={response.text}")
            
        share_meta = response.json()
        logger.info(f"Created share - received status={response.status_code} content={response.text}")
        return self.get_share_url_from_share_id( share_meta["shareid"] )

    
    
    def get_share_url(self, build_target_id: str,build_number: int) -> str:
        # fetch share id
        share_meta_url = f"{self.api_base_url}/orgs/{self.org_id}/projects/{self.project_id}/buildtargets/{build_target_id}/builds/{build_number}/share",
        response = requests.get(
                    share_meta_url,
                    headers=self.prepare_headers(),
                    timeout=fetch_timeout_secs
        )

        # responds with
        # 200{ "shareid": "-1k77srZTd",	"shareExpiry": "2022-11-30T11:57:53.448Z" }
        # 404 Error: No share found.
        if response.status_code != 200:
            raise Exception(f"Failed to get share meta from {share_meta_url} - http status={response.status_code} content={response.text}")
        share_meta = response.json()
        return self.get_share_url_from_share_id( share_meta["shareid"] )
        

@click.command()
@click.argument("api_key", envvar="UNITY_CLOUD_BUILD_API_KEY", type=str)
@click.argument("org_id", envvar="UNITY_CLOUD_BUILD_ORG_ID", type=str)
@click.argument("project_id", envvar="UNITY_CLOUD_BUILD_PROJECT_ID", type=str)
@click.argument("primary_build_target", envvar="UNITY_CLOUD_BUILD_PRIMARY_TARGET", type=str)
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
@click.argument(
    "create_share",
    envvar="UNITY_CLOUD_BUILD_CREATE_SHARE",
    type=bool,
    default=True,
)
@click.argument(
    "existing_build_number",
    envvar="UNITY_CLOUD_BUILD_USE_EXISTING_BUILD_NUMBER",
    type=int,
    default=-1,
)
@click.argument("github_head_ref", envvar="GITHUB_HEAD_REF", type=str)
@click.argument("allow_new_build_targets", envvar="UNITY_CLOUD_BUILD_ALLOW_NEW_BUILD_TARGETS", type=str, default=True)
def main(
    api_key: str,
    org_id: str,
    project_id: str,
    primary_build_target: str,
    target_platform: str,
    polling_interval: float,
    download_binary: bool,
    github_head_ref: str,
    create_share: bool,
    existing_build_number: int,
    allow_new_build_targets: bool,
) -> None:

    # validate incoming target platform
    target_platform = target_platform.lower()
    if target_platform not in platform_default_artifact_filenames.keys():
        platform_list = ", ".join(x for x in platform_default_artifact_filenames.keys())
        logger.critical(f"Target platform must be one of {platform_list}")
        sys.exit(1)

    # create unity cloud build client
    client: UnityCloudBuildClient = UnityCloudBuildClient(
        api_key,
        org_id,
        project_id,
        primary_build_target,
        target_platform,
        github_head_ref,
        allow_new_build_targets
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
            
    # for testing, use existing build number
    if existing_build_number >= 0:
        logger.info(f"Using existing build number {existing_build_number}")
        build_number = existing_build_number
    else:
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
            build_meta = client.get_build_status(build_target_id, build_number)
            # build meta is returned once the build succeeds
            if build_meta:
                break
        except tenacity.RetryError as exception:
            logger.critical(f"Unable to check status unity build {build_target_id} {build_number} after 10 attempts! {exception}")
            sys.exit(1)
        logger.info(f"Waiting {polling_interval} seconds...")
        time.sleep(polling_interval)

    logger.info(f"Build completed successfull; {build_meta}")
    
    # Build finished successfully
    if download_binary:
        artifact_meta = client.download_artifact(build_meta["links"]["download_primary"]["href"])
        write_github_output_and_env("ARTIFACT_FILENAME", artifact_meta["filename"])
        write_github_output_and_env("ARTIFACT_FILEPATH", artifact_meta["filepath"])

    # print out any sharing info to env var
    if create_share:
        share_url = client.create_share_url(build_target_id, build_number)
        logger.info(f"Got sharing url {share_url}")
        write_github_output_and_env("SHARE_URL", share_url)
            
    sys.exit(0)

if __name__ == "__main__":
    sys.exit(main())
