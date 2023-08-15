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
# if no env var, write to a local file for debugging
github_output_filename = os.getenv('GITHUB_OUTPUT') or "GITHUB_OUTPUT.txt"
github_env_filename = os.getenv('GITHUB_ENV') or "GITHUB_ENV.txt"

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


# client that just connects to unity cloud build
class UnityCloudClient:
    def __init__(
        self,
        api_key: str,
        org_id: str,
    ) -> None:
    
        logger.info("Setting up Unity Cloud Client...")
        self.api_base_url = "https://build-api.cloud.unity3d.com/api/v1"
        self.org_id = org_id.lower()
        self.api_key = api_key
        self.api_request_base_url = f"{self.api_base_url}/orgs/{self.org_id}"

    def get_request_headers(self) -> Dict:
        # get base headers for any REST requests
        return {
            "Content-Type": "application/json",
            "Authorization": f"Basic {self.api_key}",
        }
        
    def send_request(self, api_url: str) -> Dict:
        headers = self.get_request_headers()
        url = f"{self.api_request_base_url}{api_url}"
        response = requests.get( url, headers=headers, timeout=fetch_timeout_secs )
        
        if response.status_code != 200:
            raise Exception(f"Request failed with status {response.status_code} content={response.text} with url={url}")
        data = response.json()
        return data

    def post_request(self, api_url: str, post_body:Dict) -> Dict:
        headers = self.get_request_headers()
        url = f"{self.api_request_base_url}{api_url}"
        response = requests.post( url, headers=headers, timeout=fetch_timeout_secs, data=json.dumps(post_body) )
        
        if response.status_code != 200:
            raise Exception(f"Request failed with status {response.status_code} content={response.text} with url={url}")
        data = response.json()
        return data

    # List all the projects org. This is essentially to verify input settings
    def list_projects(self) -> Dict:
        logger.info(f"Fetching projects for {self.org_id}...")
        meta = self.send_request(f"/projects")
        projects = []
        for project in meta:
            projects.append( project["projectid"] )
        
        projects_string = ", ".join(projects)
        logger.info(f"Found organisation projects; {projects_string}")
        return projects
        
    # List all the build targets for this org/project. This is essentially to verify credentials/org/project settings
    def list_build_targets(self, project_id:str) -> Dict:
        logger.info(f"Fetching build targets for {self.org_id}/{project_id}...")
        meta = self.send_request(f"/projects/{project_id}/buildtargets")
        build_targets = []
        for build_target in meta:
            build_targets.append( build_target["buildtargetid"] )

        build_targets_string = ", ".join(build_targets)
        logger.info(f"Got project {project_id} build targets; {build_targets_string}")
        return build_targets

            
    def get_build_meta(self, project_id: str, build_target_id: str, build_number: int) -> None:
        #	gets the status of the running build, returns None if non-error (timeout)
        logger.info(f"Checking status of build {project_id}/{build_target_id}/{build_number}...")
        try:
            data = self.send_request(f"/projects/{project_id}/buildtargets/{build_target_id}/builds/{build_number}")
            return data
        except requests.exceptions.Timeout:
            logger.info(f"get_build_meta() timeout...")
            return None
        
    def get_successfull_build_meta(self, project_id: str, build_target_id: str, build_number: int) -> None:
        #	get build status of a build, but throw if it's failed
        #	Build status returned can be one of the below
        #	[queued, sentToBuilder, started, restarted, success, failure, canceled, unknown]
        #	We class "failure", "cancelled" and "unknown" as failures and return exit code 1.
        #	We class "success" as successful
        #	All other statuses, we return no info back (None)
        build_meta = self.get_build_meta( project_id, build_target_id, build_number )
        failed_statuses = ["failure", "canceled", "cancelled", "unknown"]
        success_statuses = ["success"]
        status = build_meta["buildStatus"]

        if status in failed_statuses:
            raise Exception(f"Build {project_id}/{build_target_id}/{build_number} failed with status: {status}; meta={build_meta}")

        if status in success_statuses:
            logger.info(f"Build {project_id}/{build_target_id}/{build_number} completed successfully!")
            return build_meta
                
        logger.info(f"Build {project_id}/{build_target_id}/{build_number} is still running: {status}; meta={build_meta}")
        return None

    def get_share_url_from_share_id(self, share_id:str ) -> str:
        return f"https://developer.cloud.unity3d.com/share/share.html?shareId={share_id}"

    def create_share_url(self, project_id:str, build_target_id: str,build_number: int) -> str:
        # if a share already exists for this build, it will be revoked and a new one created (note: same url as GET share meta)
        create_share_url = f"/projects/{project_id}/buildtargets/{build_target_id}/builds/{build_number}/share"
        post_body = {'shareExpiry':''}
        share_meta = self.post_request( create_share_url, post_body )
        logger.info(f"Created share {share_meta}")
        return self.get_share_url_from_share_id( share_meta["shareid"] )
    
    
    def get_share_url(self, project_id:str, build_target_id: str,build_number: int) -> str:
        # fetch share id
        share_meta = self.send_request(f"/projects/{project_id}/buildtargets/{build_target_id}/builds/{build_number}/share")
        # responds with
        # 200{ "shareid": "-1k77srZTd",	"shareExpiry": "2022-11-30T11:57:53.448Z" }
        # 404 Error: No share found.
        return self.get_share_url_from_share_id( share_meta["shareid"] )
        
        
        
        
# Handles build target setup & build execution to a client
class UnityCloudBuilder:
    def __init__(
        self,
        client: UnityCloudClient,
        project_id: str,
        primary_build_target: str,
        github_branch_ref: str,
        github_head_ref: str,
        github_commit_sha: str,
        allow_new_build_targets: bool,
    ) -> None:
        
        if not github_branch_ref:
            raise Exception(f"Missing github_branch_ref. required")

        self.is_pull_request = github_branch_ref.startswith("refs/pull/")

        # The github_branch_ref is now always required, and will be checked against the default target's configuration
        # new build targets will then be created, for pull requests, new branches, tags etc
        self.branch_ref = github_branch_ref
        self.commit_sha = github_commit_sha
        self.head_ref = github_head_ref or ""
        
        # need to strip branch name down to what will be passed to git clone --branch XXX in unity cloud build
        # gr: unity runs git clone --branch xxxx
        #		it does work for tags --branch v0.0.1
        #		this does NOT work for pull requests; refs/pull/6/merge; refs/tag/xxx
        #		for pull requests, we need to use head_ref
        self.branch_name = self.branch_ref
        self.branch_name = self.branch_name.replace("refs/tags/", "")
        self.branch_name = self.branch_name.replace("refs/heads/", "")
        self.branch_name = self.branch_name.replace("refs/pull/", "pull request ")
        # strip /merge from refs/pull/666/merge to be pretty
        if self.is_pull_request:
            self.branch_name = self.branch_name.replace("/merge", "")

        # strip the head ref down to a branch, in case we use it
        self.head_ref = self.head_ref.replace("refs/heads/", "")

        if self.is_pull_request and not self.head_ref:
            raise Exception(f"Detected pull request from {github_branch_ref} but missing github_head_ref '{self.head_ref}' which should be the source branch")

        self.allow_new_build_targets = allow_new_build_targets

        logger.info("Setting up Unity Cloud Client...")
        self.project_id = project_id.lower()
        self.primary_build_target_id = primary_build_target.lower()
        self.client = client

    
    def get_build_target(self, build_target_id: str) -> Dict:
        """
        Looks up a build target from Unity Cloud Build
        """
        logger.info(f"Fetching build target meta for {build_target_id}...")
        resp = requests.get(
            f"{self.api_base_url}/orgs/{self.org_id}/projects/{self.project_id}/buildtargets/{build_target_id}",
            headers=self.client.get_request_headers(),
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
            headers=self.client.get_request_headers(),
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
        # Creates a new build target in Unity Cloud Build if we are dealing with a pull request.
        # Otherwise we return the primary build target (user's original configuration)
	
        # get primary build target so that we can copy across all relevant settings to our PR target
        # always fetch it, to validate if the user-input target id is correct
        primary_build_target = self.get_build_target(self.primary_build_target_id)
        logger.info(f"Primary Build Target Meta: {primary_build_target}")
        primary_build_branch = primary_build_target["settings"]["scm"]["branch"]
        
        is_primary_build_target_branch_match = self.branch_name == primary_build_branch
        
        if not is_primary_build_target_branch_match:
            logger.info(f"Building branch {self.branch_name} doesn't match primary build target branch {primary_build_branch}, creating new target")
            
            # todo: find existing build target with matching branch name, with matching configuration settings?
            if not self.allow_new_build_targets:
                raise Exception(f"Creating new build targets not allowed")

            # replace any special chars and ensure length is max of 56 chars
            # 64 is the limit, but we allow some free chars for platform
            # todo: just do 64-(prefix-length)
            new_target_name = re.sub("[^0-9a-zA-Z]+", "-", self.branch_name)
            new_target_name = f"{self.primary_build_target_id}-{new_target_name}"
            # 64 char limit for targets (citation needed)
            new_target_name = new_target_name[:63]
            # targets must be lower case
            new_target_name = new_target_name.lower()

            logger.info(f"Creating new build target({new_target_name}) for {self.project_id} for branch {self.branch_name}...")

            # setup new payload copying relevant settings from the primary build target
            payload = {
                "name": new_target_name,
                "enabled": True,
                "platform": primary_build_target["platform"],
                "settings": primary_build_target["settings"],
                "credentials": primary_build_target["credentials"],
            }

            # override payload settings with our PR branch name and remove any applied build schedules.
            # gr: if branch is refs/pull/XX/[merge|head], this will fail as unity will do git clone -branch refs/pull/6/merge which
            #		isn't possible. So we need to checkout the commit (or head of PR branch?) instead
            if self.is_pull_request:
                payload["settings"]["scm"]["branch"] = self.head_ref
            else:
                payload["settings"]["scm"]["branch"] = self.branch_name
            payload["settings"]["buildSchedule"] = {}

            # make the new build target
            resp = requests.post(
                f"{self.api_base_url}/orgs/{self.org_id}/projects/{self.project_id}/buildtargets",
                headers=self.client.get_request_headers(),
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
                    logger.info(f"Build target for this PR already exists: {new_target_name}. Re-using...")
                    return new_target_name
            raise Exception(f"Build target could not be created - received status={resp.status_code} content={resp.text}")

        # otherwise return the primary build target (user's original configuration)
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
            headers=self.client.get_request_headers(),
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





def download_file_to_workspace(url: str) -> Dict:
	logger.info(f"Downloading file to workspace ({GITHUB_WORKSPACE})... {url}")
	
	response = requests.get(url, allow_redirects=True)
	if response.status_code != 200:
		raise Exception(f"Request failed with status {response.status_code} content={response.text} with url={url}")

	#	find filename from response
	filename = response.headers.get('content-disposition') or ""
	if filename.startswith("attachment; filename="):
		filename = filename.replace("attachment; filename=","")
	else:
		raise Exception("Dont know how to get filename from url/response (no content-disposition")

	# files must be written inside the GITHUB_WORKSPACE
	filepath = Path( GITHUB_WORKSPACE ) / "artifacts" / filename
	filepath_absolute = filepath.absolute()
	if not filepath.is_relative_to(GITHUB_WORKSPACE):
		logger.warning(f"filepath({filepath_absolute} is not relative to GITHUB_WORKSPACE({GITHUB_WORKSPACE})")

	directory = filepath.parent
	directory.mkdir(parents=True,exist_ok=True)
	try:
		logger.info(f"Writing download to {filepath_absolute}...")
		filepath.write_bytes(response.content)
	except IOError as exception:
		raise Exception(f"Could not file download to disk ({filepath_absolute}): {exception}")

	# need to output a github_workspace relative path
	workspacefilepath = str( filepath.relative_to(GITHUB_WORKSPACE) )
	meta = { "filename":filename, "filepath":workspacefilepath}
	logger.info(f"Download to {meta['filepath']} successful!")
	return meta


# start a new build and return dictionary with
#	.build_number ; for monitoring
#	.build_target_id ; build target id in case a new one was created
def create_new_build(
    client: UnityCloudClient,
    project_id: str,
    primary_build_target: str,
    polling_interval: float,
    github_branch_ref: str,
    github_head_ref: str,
    github_commit_sha: str,
    allow_new_target: bool,
) -> Dict:

    # create unity cloud build client
    builder: UnityCloudBuilder = UnityCloudBuilder(
        client,
        project_id,
        primary_build_target,
        github_branch_ref,
        github_head_ref,
        github_commit_sha,
        allow_new_build_targets
    )
    client = builder



        
    # obtain the build target we need to run against
    try:
        build_target_id: str = builder.get_build_target_id()
        logger.info(f"Acquired Build Target Id: {build_target_id}. Primary Target Id: {primary_build_target}")
    except BaseException as exception:
        logger.critical(f"Unable to obtain unity build target; {exception}")
        sys.exit(1)

    if build_target_id != primary_build_target:

        # create a new build for the specified build target
        try:
            build_number = builder.start_build(build_target_id)
            logger.info(f"Started build number {build_number}")
        except tenacity.RetryError:
            logger.critical(
                f"Unable to start unity build {build_target_id} after 10 attempts!"
            )
            sys.exit(1)




@click.command()
@click.option("--api_key", envvar="UNITY_CLOUD_BUILD_API_KEY", type=str)
@click.option("--org_id", envvar="UNITY_CLOUD_BUILD_ORG_ID", type=str)
@click.option("--project_id", envvar="UNITY_CLOUD_BUILD_PROJECT_ID", type=str)
@click.option("--primary_build_target", envvar="UNITY_CLOUD_BUILD_PRIMARY_TARGET", type=str)
@click.option(
    "--polling_interval",
    envvar="UNITY_CLOUD_BUILD_POLLING_INTERVAL",
    type=float,
    default=60.0,
)
@click.option(
    "--download_binary",
    envvar="UNITY_CLOUD_BUILD_DOWNLOAD_BINARY",
    type=bool,
    default=False,
)
@click.option(
    "--create_share",
    envvar="UNITY_CLOUD_BUILD_CREATE_SHARE",
    type=bool,
    default=False,
)
@click.option(
    "--existing_build_number",
    envvar="UNITY_CLOUD_BUILD_USE_EXISTING_BUILD_NUMBER",
    type=int,
    default=None,
)
@click.option("--github_branch_ref", envvar="UNITY_CLOUD_BUILD_GITHUB_BRANCH_REF", type=str)
@click.option("--github_head_ref", envvar="UNITY_CLOUD_BUILD_GITHUB_HEAD_REF", type=str)
@click.option("--github_commit_sha", envvar="UNITY_CLOUD_BUILD_GITHUB_COMMIT_SHA", type=str)
@click.option("--allow_new_build_targets", envvar="UNITY_CLOUD_BUILD_ALLOW_NEW_BUILD_TARGETS", type=str, default=True)
def main(
    api_key: str,
    org_id: str,
    project_id: str,
    primary_build_target: str,
    polling_interval: float,
    download_binary: bool,
    github_branch_ref: str,
    github_head_ref: str,
    github_commit_sha: str,
    create_share: bool,
    existing_build_number: int,
    allow_new_build_targets: bool
) -> None:

    # sanitise some inputs
    if project_id:
        project_id = project_id.lower()
    if primary_build_target:
        primary_build_target = primary_build_target.lower()


    client: UnityCloudClient = UnityCloudClient(
        api_key,
        org_id
    )

    # to help users and for debug, just list all projects
    projects = client.list_projects()


    # when we have an existing build number, we don't need a lot of the other meta
    # but we do need the project it belongs to
    # do this AFTER listing projects, so user can see a project id they might be looking for
    # todo: if user supplied build number AND other meta, validate that meta and throw if there's a mismatch
    if existing_build_number != None and project_id == None:
        raise Exception(f"existing_build_number({existing_build_number}) supplied, but missing required project_id({project_id})")

    # if the user has provided a project, list it's build targets, otherwise, list em all!
    if project_id:
        client.list_build_targets( project_id )
    else:
        for project in projects:
            client.list_build_targets( project )

    if existing_build_number != None and primary_build_target == None:
        raise Exception(f"existing_build_number({existing_build_number}) supplied, but missing required primary_build_target({primary_build_target})")


    # get variables we're eventually going to use
    build_number = None
    build_target_id = None
    
    # when we have an existing build number, we don't need a lot of the other meta
    # todo: if user supplied build number AND other meta, validate that meta and throw if there's a mismatch
    if existing_build_number != None:
       build_number = existing_build_number
       build_target_id = primary_build_target
       logger.info(f"Using existing build target/number {build_target_id}/{build_number}...")
       
    else:
        build_meta = create_new_build(
            client,
            project_id,
            primary_build_target,
            polling_interval,
            github_branch_ref,
            github_head_ref,
            github_commit_sha,
            allow_new_build_targets
            )
        build_number = build_meta["build_number"]
        build_target_id = build_meta["build_target_id"]

    # poll the running build for updates waiting for an polling interval between each poll
    while True:
        # todo: make this function print out erroring logs
        build_meta = client.get_successfull_build_meta( project_id, build_target_id, build_number )
        # build meta is returned once the build succeeds
        if build_meta:
            break
        logger.info(f"Waiting {polling_interval} seconds...")
        time.sleep(polling_interval)

    #logger.info(f"Build completed successfully; {build_meta}")
    
    # Build finished successfully
    if download_binary:
        artifact_meta = download_file_to_workspace(build_meta["links"]["download_primary"]["href"])
        write_github_output_and_env("ARTIFACT_FILENAME", artifact_meta["filename"])
        write_github_output_and_env("ARTIFACT_FILEPATH", artifact_meta["filepath"])

    # print out any sharing info to env var
    if create_share:
        share_url = client.create_share_url( project_id, build_target_id, build_number )
        logger.info(f"Got sharing url {share_url}")
        write_github_output_and_env("SHARE_URL", share_url)
            
    sys.exit(0)

if __name__ == "__main__":
    sys.exit(main())
