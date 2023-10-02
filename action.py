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


#	logger.info(f"Json, but pretty {pretty_json(Dictionary)}")
def pretty_json(Json:dict):
	return json.dumps(
		Json,
		sort_keys=False,
		indent=4,
		separators=(',', ': ')
	)





class BranchAndLabel:
	def __init__(
		self,
		branch: str,
		label: str,
	) -> None:
		self.branch = branch
		self.label = label


#	given git inputs (branch_ref and/or head_ref) - filter down to the branch we want to use
#	return a .branch...
#		- that  branch is used by unity github with
#			git clone --branch xxxx
#		- it does work for tags --branch v0.0.1
#		- this does NOT work for pull requests; refs/pull/6/merge; refs/tag/xxx
#		- so for pull requests, we need to use head_ref (refs/heads/xyz)
#	and return a .label...
#		that will be used as the name of the build target
def get_branch_and_label(branch_ref,head_ref):
	
	# gr: is this redundant, and always true if head_ref != empty?
	is_pull_request = branch_ref.startswith("refs/pull/")

	if is_pull_request and not head_ref:
		raise Exception(f"Detected pull request from {branch_ref} but missing github_head_ref '{head_ref}' which should be the source branch")

	# need to strip branch name down to what will be passed to git clone --branch XXX in unity cloud build
	# 	git clone --branch xxxx
	#		this does NOT work for pull requests; refs/pull/6/merge; refs/tag/xxx
	#		for pull requests, we need to use head_ref
	branch = branch_ref
	branch = branch.replace("refs/tags/", "")
	branch = branch.replace("refs/heads/", "")
	branch = branch.replace("refs/pull/", "pull request ")

	# for pull requests the label wants to be branch_ref to indicate it's a pr
	label = branch

	# strip the head ref down to a branch, in case we use it
	head_ref = head_ref or ""
	head_ref = head_ref.replace("refs/heads/", "")

	if is_pull_request:
		branch = head_ref

	return BranchAndLabel( branch, label )



#	get a sanitised target name for unity cloud build
#	note: this SHOULD use primary_build_target if the branch is the same as the primary target's branch
#		ie. we dont get "mac-main", just "mac"
#		but at this point we are still dealing with strings
def get_build_targetname(primary_build_target,branch_and_label):
	
	if not primary_build_target:
		raise Exception(f"get_build_target_name() requires primary_build_target")
	if not branch_and_label:
		raise Exception(f"get_build_target_name() requires branch_and_label")

	#	sanitise label for unity cloud build's restrictions
	target_name = branch_and_label.label
	# replace any special chars and ensure length is max of 56 chars
	# 64 is the limit, but we allow some free chars for platform
	# todo: just do 64-(prefix-length)
	target_name = re.sub("[^0-9a-zA-Z]+", "-", target_name)
	target_name = f"{primary_build_target}-{target_name}"
	# 64 char limit for targets (citation needed)
	target_name = target_name[:63]
	# targets must be lower case
	target_name = target_name.lower()
	
	return target_name
	
	


# client that just connects to unity cloud build and does standard calls
# no project/builder specific functionality in here!
# custom code like "nice build names from pull releases" should be somewhere else (UnityCloudBuilder)
class UnityCloudClient:
    def __init__(
        self,
        api_key: str,
        org_id: str,
    ) -> None:
    
        if not org_id:
            raise Exception(f"Org_id missing({org_id})")

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

    def post_request(self, api_url: str, post_body:Dict, success_codes=[200]) -> Dict:
        headers = self.get_request_headers()
        url = f"{self.api_request_base_url}{api_url}"
        response = requests.post( url, headers=headers, timeout=fetch_timeout_secs, data=json.dumps(post_body) )
        
        if not response.status_code in success_codes:
            raise Exception(f"Request failed with status {response.status_code}(not {success_codes}) content={response.text} with url={url}")
            
        data = response.json()
        # insert response code
        if isinstance(data,dict):
            data["post_request_response_status_code"] = response.status_code
        else:
            print(f"request responded with non-dictionary; {type(data)}")
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

        #build_targets_string = ", ".join(build_targets)
        #logger.info(f"Got project {project_id} build targets; {build_targets_string}")
        return build_targets

            
    def get_build_meta(self, project_id: str, build_target_name: str, build_number: int) -> None:
        #	gets the status of the running build, returns None if non-error (timeout)
        logger.info(f"Checking status of build {project_id}/{build_target_name}/{build_number}...")
        data = self.send_request(f"/projects/{project_id}/buildtargets/{build_target_name}/builds/{build_number}")
        return data
        
        
    def get_successfull_build_meta(self, project_id: str, build_target_name: str, build_number: int) -> None:
        #	get build status of a build, but throw if it's failed
        #	Build status returned can be one of the below
        #	[queued, sentToBuilder, started, restarted, success, failure, canceled, unknown]
        #	We class "failure", "cancelled" and "unknown" as failures and return exit code 1.
        #	We class "success" as successful
        #	All other statuses, we return no info back (None)
        build_meta = self.get_build_meta( project_id, build_target_name, build_number )

        failed_statuses = ["failure", "canceled", "cancelled", "unknown"]
        success_statuses = ["success"]
        status = build_meta["buildStatus"]

        if status in failed_statuses:
            raise Exception(f"Build {project_id}/{build_target_name}/{build_number} failed with status: {status}; meta={build_meta}")

        if status in success_statuses:
            logger.info(f"Build {project_id}/{build_target_name}/{build_number} completed successfully!")
            return build_meta
                
        logger.info(f"Build {project_id}/{build_target_name}/{build_number} is still running: {status}; meta={pretty_json(build_meta)}")
        return None

    def get_share_url_from_share_id(self, share_id:str ) -> str:
        return f"https://developer.cloud.unity3d.com/share/share.html?shareId={share_id}"

    def create_share_url(self, project_id:str, build_target_name: str,build_number: int) -> str:
        # if a share already exists for this build, it will be revoked and a new one created (note: same url as GET share meta)
        create_share_url = f"/projects/{project_id}/buildtargets/{build_target_name}/builds/{build_number}/share"
        post_body = {'shareExpiry':''}
        share_meta = self.post_request( create_share_url, post_body )
        logger.info(f"Created share {share_meta}")
        return self.get_share_url_from_share_id( share_meta["shareid"] )
    
    
    def get_share_url(self, project_id:str, build_target_name: str,build_number: int) -> str:
        # fetch share id
        share_meta = self.send_request(f"/projects/{project_id}/buildtargets/{build_target_name}/builds/{build_number}/share")
        # responds with
        # 200{ "shareid": "-1k77srZTd",	"shareExpiry": "2022-11-30T11:57:53.448Z" }
        # 404 Error: No share found.
        return self.get_share_url_from_share_id( share_meta["shareid"] )

    # List all the build[number]s for a target, in a project
    def list_build_numbers(self, project_id:str, build_target:str) -> Dict:
        logger.info(f"Fetching builds for build target {build_target} for {self.org_id}/{project_id}...")
        meta = self.send_request(f"/projects/{project_id}/buildtargets/{build_target}/builds")
        #logger.info(f"Got builds from target; {meta}")
        buildnumbers = {}
        for build_meta in meta:
            buildnumber = build_meta["build"]
            status = build_meta["buildStatus"]
            buildnumbers[buildnumber] = status

        return buildnumbers

    def get_build_target_meta(self, project_id:str, build_target_name: str) -> Dict:
        #	Looks up a build target from Unity Cloud Build
        logger.info(f"Fetching build target meta for target={project_id}/{build_target_name}...")
        target_meta = self.send_request(f"/projects/{project_id}/buildtargets/{build_target_name}")
        return target_meta






# Handles build target setup & build execution to a client
class UnityCloudBuilder:
    def __init__(
        self,
        client: UnityCloudClient,
        project_id: str,
        primary_build_target: str,
        branch_and_label: BranchAndLabel
    ) -> None:
        if not project_id:
            raise Exception(f"Missing project_id. required")
        if not client:
            raise Exception(f"Missing client. required")
        if not primary_build_target:
            raise Exception(f"Missing primary_build_target. required")
        if not branch_and_label:
            raise Exception(f"Missing branch_and_label. required")

        self.project_id = project_id
        self.primary_build_target = primary_build_target
        self.branch_and_label = branch_and_label
        self.client = client


    def get_build_targetname(self):
        # if the primary target's branch is the same as the branch we're using
        # then the build target is the primary target
        primary_target_meta = self.client.get_build_target_meta( self.project_id, self.primary_build_target )
        primary_build_branch = primary_target_meta["settings"]["scm"]["branch"]
        if primary_build_branch == self.branch_and_label.branch:
            return self.primary_build_target

        target_name = get_build_targetname( self.primary_build_target, self.branch_and_label )
        return target_name

    @tenacity.retry(
        wait=tenacity.wait_exponential(multiplier=1, min=4, max=10),
        stop=tenacity.stop_after_attempt(10),
        after=tenacity.after_log(logger, logging.DEBUG)
    )
    def set_build_target_env_var(
        self, build_target_name: str, key: str, value: str
    ) -> None:
        """
        sets an env var to a build target.

        TODO: If we need more than a couple of env vars- we should update this func to set vars in bulk
        """
        logger.info(f"Setting env var: {key} on target: {build_target_name}...")
        resp = requests.put(
            f"{self.api_base_url}/orgs/{self.org_id}/projects/{self.project_id}/buildtargets/{build_target_name}/envvars",
            headers=self.client.get_request_headers(),
            json={key: value},
        )
        if resp.status_code == 200:
            return
        raise Exception(f"Env var could not be set - received status={resp.status_code} content={resp.text}")




    def get_build_target_meta(self,allow_new_target:bool):
        build_target_name = self.get_build_targetname()
        
        try:
            existing_meta = self.client.get_build_target_meta( self.project_id, build_target_name )
            return existing_meta
        except Exception as e:
            logger.info(f"No build target meta for {build_target_name}... {e}")
        
        if not allow_new_target:
            raise Exception(f"No built target for {build_target_name} and not allowed to create a new one")
        
        return self.create_new_build_target(build_target_name)


    # returns meta of new target
    def create_new_build_target(self,build_target_name:str) -> str:
        #	clone the primary build target and set up to create a new one on self's branch
        #	branch and label are already calculated
        
        #	get primary build target meta
        logger.info(f"Fetching Primary Build Target Meta: {self.primary_build_target}...")
        primary_target_meta = self.client.get_build_target_meta( self.project_id, self.primary_build_target )
        logger.info(f"Primary Build Target Meta: {pretty_json(primary_target_meta)}")
        
        logger.info(f"Creating new build target({build_target_name}) for branch {self.branch_and_label.branch}...")

        # setup new payload copying relevant settings from the primary build target
        payload = {
            "name": build_target_name,
            "enabled": True,
            "platform": primary_target_meta["platform"],
            "settings": primary_target_meta["settings"],
            "credentials": primary_target_meta["credentials"],
        }
        
        #	the branch here is used with git clone --branch XXX
        #	see get_branch_and_label()
        payload["settings"]["scm"]["branch"] = self.branch_and_label.branch

        #	reset some other settings
        payload["settings"]["buildSchedule"] = {}

        # make the new build target
        # unity returns 201 when we get a new config
        # 500 -> .error == "Build target name already in use for this project!"
        # means that our generated name already exists, we want to catch & reuse that
        #	gr: the new version of the code should probably fail here, as we should have already checked if it exists
        success_codes = [201,500]
        new_target_meta = self.client.post_request(f"/projects/{self.project_id}/buildtargets", payload, success_codes )
        
        print(f"new_target_meta = {new_target_meta}")
        if new_target_meta["post_request_response_status_code"] == 500:
            error = new_target_meta["error"]
            if error == "Build target name already in use for this project!":
                logger.info(f"Build target for this branch already exists: {new_target_name}. Re-using...")
            else:
                raise Exception(f"New target had error: {error}")

        return new_target_meta

    def start_build(self, build_target_name: str) -> int:
        # Kicks off a new build of the project for the correct build target
        clean = False
        logger.info(f"Creating a build of {self.project_id} on target {build_target_name} (clean={clean})...")

        post_body = {"clean": clean, "delay": 0}
        start_build_url = f"/projects/{self.project_id}/buildtargets/{build_target_name}/builds"
        orig_build_meta = self.client.post_request( start_build_url, post_body, [202] )
        build_meta = orig_build_meta[0]
        error = build_meta.get("error")
        if error:
            logger.warning(f"New build response has error: {error}")

        if not "build" in build_meta:
            if not error:
                error = orig_build_meta
            raise Exception(f"start_build response has no build number; error={error}")

        build_number = build_meta["build"]
        logger.info(f"Build {build_number} created successfully!")
        return build_number


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


def is_useful_build_meta_key(key: str):
	if "InSeconds" in key:
		return True
	if "tatus" in key:
		return True

	return False

def wait_for_successfull_build(client: UnityCloudClient, project_id:str, build_target_name:str, build_number:int, polling_interval:float ):
	
	#	gr: reset the timeout count whenever there's a successfull response so we stick around over blips
	timeout_count = 0
	max_timeouts_in_a_row = 6
	
	while timeout_count < max_timeouts_in_a_row:
		time.sleep(polling_interval)

		try:
			build_meta = client.get_build_meta( project_id, build_target_name, build_number )
			# non-erroring fetch, so reset timeout
			timeout_count = 0

		# catch timeouts, but anything else should throw
		except requests.exceptions.Timeout:
			logger.info(f"Timeout fetching build meta ({timeout_count}/{max_timeouts_in_a_row} tries). Waiting {polling_interval} secs...")
			timeout_count = timeout_count+1
			continue

		#	print out useful information!
		failed_statuses = ["failure", "canceled", "cancelled", "unknown"]
		success_statuses = ["success"]
		status = build_meta["buildStatus"]

		useful_meta = {}
		for key,value in build_meta.items():
			if is_useful_build_meta_key(key):
				useful_meta[key] = value

		if status in success_statuses:
			logger.info(f"Build {project_id}/{build_target_name}/{build_number} completed successfully; {pretty_json(useful_meta)}")
			return build_meta

		if status in failed_statuses:
			logger.info(f"Build {status} meta: {pretty_json(build_meta)}")
			raise Exception(f"Build {project_id}/{build_target_name}/{build_number} failed with status: {status}")

		logger.info(f"Build not finished ({status})... {pretty_json(useful_meta)}")



@click.command()
@click.option("--api_key", envvar="UNITY_CLOUD_BUILD_API_KEY", type=str)
@click.option("--org_id", envvar="UNITY_CLOUD_BUILD_ORG_ID", type=str)
@click.option("--project_id", envvar="UNITY_CLOUD_BUILD_PROJECT_ID", type=str)
@click.option("--primary_build_target", envvar="UNITY_CLOUD_BUILD_PRIMARY_TARGET", type=str)
@click.option(
    "--polling_interval",
    envvar="UNITY_CLOUD_BUILD_POLLING_INTERVAL",
    type=float,
    default=10.0,
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
@click.option("--allow_new_target", envvar="UNITY_CLOUD_BUILD_ALLOW_NEW_TARGET", type=bool, default=True)
def main(
    api_key: str,
    org_id: str,
    project_id: str,
    primary_build_target: str,
    polling_interval: float,
    download_binary: bool,
    github_branch_ref: str,
    github_head_ref: str,
    create_share: bool,
    existing_build_number: int,
    allow_new_target: bool
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
        build_targets = client.list_build_targets( project_id )
        logger.info(f"Project {project_id} build targets; {pretty_json(build_targets)}")
    else:
        for project in projects:
            build_targets = client.list_build_targets( project )
            logger.info(f"Project {project} build targets; {pretty_json(build_targets)}")

    if not project_id:
        raise Exception(f"No project_id specified, don't know what to build/fetch")

    if existing_build_number != None and primary_build_target == None:
        raise Exception(f"existing_build_number({existing_build_number}) supplied, but missing required primary_build_target({primary_build_target})")

    if not primary_build_target:
        raise Exception(f"Missing primary_build_target. required")

    if not github_branch_ref:
        logger.info(f"No github_branch_ref specified. required to find build target")


    # run the branch_refs to config.branch & label functions early to check we're doing it right
    branch_and_label = get_branch_and_label( github_branch_ref, github_head_ref )
    logger.info(f"Input branch refs -> label={branch_and_label.label} branch={branch_and_label.branch} (from github_branch_ref={github_branch_ref} github_head_ref={github_head_ref})")

    builder: UnityCloudBuilder = UnityCloudBuilder(
        client,
        project_id,
        primary_build_target,
        branch_and_label
        )

    # use builder.get_build_targetname to resolve mac-main to just mac (if main is the branch of the primary target)
    #build_target_name = get_build_targetname( primary_build_target, branch_and_label )
    build_target_name = builder.get_build_targetname()
    logger.info(f"Input -> build_target_name={build_target_name}")

    if allow_new_target == False:
        logger.info(f"allow_new_target={allow_new_target}, listing all builds for {build_target_name})")
        build_numbers = client.list_build_numbers( project_id, build_target_name )
        logger.info(f"build_numbers = {pretty_json(build_numbers)}")
        return
    
    logger.info(f"use allow_new_target=false to list all existing builds for {build_target_name}")


    # get variables we're eventually going to use
    build_number = None
    
    # when we have an existing build number, we don't need a lot of the other meta
    # todo: if user supplied build number AND other meta, validate that meta and throw if there's a mismatch
    if existing_build_number != None:
       build_number = existing_build_number
       logger.info(f"Using existing build target/number {build_target_name}/{build_number}...")
       
    else:
       logger.info(f"Get/Create new build target... {build_target_name}")
       # obtain the build target we need to run against
       # this will create a new target if it doesnt exist
       build_meta = builder.get_build_target_meta( allow_new_target )
       
       # create a new build for the specified build target
       build_number = builder.start_build(build_target_name)
       logger.info(f"Started build number {build_number} on {build_target_name}")

    #	poll the running build for updates waiting for an polling interval between each poll
    #	throws if the build wasn't successfull
    build_meta = wait_for_successfull_build( client, project_id, build_target_name, build_number, polling_interval )
    
    # Build finished successfully
    if download_binary:
        artifact_meta = download_file_to_workspace(build_meta["links"]["download_primary"]["href"])
        write_github_output_and_env("ARTIFACT_FILENAME", artifact_meta["filename"])
        write_github_output_and_env("ARTIFACT_FILEPATH", artifact_meta["filepath"])

    # print out any sharing info to env var
    if create_share:
        share_url = client.create_share_url( project_id, build_target_name, build_number )
        logger.info(f"Got sharing url {share_url}")
        write_github_output_and_env("SHARE_URL", share_url)
            
    sys.exit(0)

if __name__ == "__main__":
    sys.exit(main())
