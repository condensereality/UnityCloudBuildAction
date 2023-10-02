Run Unity Cloud Build Action
==============================

This actions allows us to run Unity Cloud Build jobs from GitHub on pull request and merge to main branch.
The action uses the Unity Cloud Build REST API to kick off a build and then polls it until the job is complete.
If the job fails, the action will return an exit code 1. If it succeeds it will return an exit code 0.

For pull requests, the action will create a new build target in Unity Cloud Build first before kicking off a build.
We have to do this because Unity Cloud Build does not support overridding the branch name at runtime. So if we didn't
do this, all the build would be run against the main branch and not the changes in the PR branch - not ideal for a pull request.

If a PR has further changes to pushed to it after initially being opened, the action will re-use the previous setup PR build target.
Doing this allows us to make use of Unity Cloud Build's build target caching mechanism which speeds up subsequent builds slightly.

Action Script Usage
-----------------
Use the following commands to extract all the information required from UnityCloudBuild=st
- Run with just `org_id`, to get a list of projects & their build targets
	- `poetry run python -m action --api_key=YOUR_KEY --org_id=YOUR_ORG`
- Run with `org_id` and `project_id` to get a list of build targets
	- `poetry run python -m action --api_key=YOUR_KEY --org_id=YOUR_ORG --project_id=YourProjectName`
- Run with `org_id`, `project_id`, `primary_build_target` and `github_branch_ref` to get a list of existing builds (for use with `existing_build_number`)
	- `poetry run python -m action --api_key=YOUR_KEY --org_id=YOUR_ORG --project_id=YourProjectName --primary_build_target=mac --github_branch_ref=refs/tags/v1.2.3 --allow_new_target=false`


Debugging
----------------
- Don't hesitate to use Unity's own unity-cloud-build-api checker; [https://build-api.cloud.unity3d.com/docs/1.0.0/index.html](https://build-api.cloud.unity3d.com/docs/1.0.0/index.html)
- The action will list out all projectids in an organisation, and if that is successfull, all build targets for a projectid. Check the logs!
- `403 User not authorised` is a common error for when organisationids or projectids are incorrect.
- `API_KEY missing` error from `action.py` often means you have provided a secret which doesn't exist, or is empty.


Inputs
--------

The action requires a series of inputs from the workflow file. These are then passed to the action as environment variables that the
action then picks up and utilises to make relevant calls to Unity Cloud Build.

### `unity_cloud_build_api_key`
- `Required`  
- The Unity Cloud Build API key. This can be found in the Unity Dashboard, under `Dev Ops -> Build Automation -> Settings` 
- See [https://build-api.cloud.unity3d.com/docs/1.0.0/index.html](https://build-api.cloud.unity3d.com/docs/1.0.0/index.html)
- Store this API key as a `github action secret` and then use in the github action via `${{ secrets.NAME_OF_YOUR_SECRET }}`

### `unity_cloud_build_org_id`
- `Required`  
- The Unity Cloud Build organisation ID 
- this is can be found by [browsing your organisations](https://id.unity.com/en/organizations/) and finding the organisation name in any links/urls. (expected to be a string)
- Organisation id's will fail if they have spaces, and UnityCloudBuild expects spaces to be replaced with dashes; https://forum.unity.com/threads/ucb-api-error-not-authorized-user-does-not-have-correct-permissions-to-perform-this-operation.730976/

### `unity_cloud_build_project_id`
- `Required`  
- The Unity Cloud Build project ID
- Unity will fail if the project id has spaces in it, these should be replaced with dashes
- `My Project` has a project id of `my-project`

### `unity_cloud_build_primary_target`
- `Required`  
- The Unity Cloud Build primary build target - this is build target name. (Find this via urls, expected to be without spaces)

### `unity_cloud_build_polling_interval`
- `Optional
- The frequency with which to query Unity Cloud Build jobs - the default is ``60`` seconds.

### `unity_cloud_build_download_binary`
- `optional`
- default: `false`
- output: `env.ARTIFACT_FILENAME` is set to the filename of the local file after download
- output: `env.ARTIFACT_FILEPATH` is set to the full file path of the local file after download
- Whether or not to download the built binary to your ``GITHUB_WORKSPACE``

### `unity_cloud_build_create_share`
- `optional`
- default: `false`
- output: `env.SHARE_URL` is set to the sharing url
- Tell UnityCloudBuild to generate a sharing url to allow easy installation


### `unity_cloud_build_use_existing_build_number`
- `optional`
- default: `-1`
- Instead of starting a new build, if this is >=0 the action will use an existing build number and still execute downloading artifacts, creating share urls etc


## Example usage

Please find an example usage below for reference.

```yaml
jobs:
  run-unity-cloud-build-condense-live-android:
    name: Run Unity Cloud Build - Condense Live (Android)
    runs-on: ubuntu-22.04
    outputs:
       artifact_filepath: ${{ steps.rununitycloudbuildaction.ARTIFACT_FILEPATH }}
    steps:
      - name: Checkout
        uses: actions/checkout@v2
      - name: Install Python 3
        uses: actions/setup-python@v4
        with:
          python-version: "3.10.4"
          architecture: "x64"
      - name: Export poetry.lock for docker build
        run: |
          python -m pip install --upgrade pip
          pip install poetry==1.1.12
          cd .github/actions/unity-cloud-build/ && poetry export -f requirements.txt > requirements.txt
      - name: Run Unity Cloud Build Action
        uses: ./.github/actions/unity-cloud-build
        id: rununitycloudbuildaction
        with:
          unity_cloud_build_api_key: ${{ secrets.UNITY_CLOUD_BUILD_API_KEY }}
          unity_cloud_build_org_id: yourorganisation
          unity_cloud_build_project_id: yourgame
          unity_cloud_build_polling_interval: 60
          unity_cloud_build_primary_target: yourgame-ios-default
          unity_cloud_build_download_binary: false
          unity_cloud_build_create_share: true
          unity_cloud_build_github_head_ref: ${{ github.ref }}
          
```

Outputs
-------------
This action outputs several `env` vars to relay meta back out to the workflow. 
These are also written to `GITHUB_OUTPUT` for use via `needs.job.output` and `steps.step.outputs.xxx`
- `ARTIFACT_FILEPATH` local path to Unity Cloud Build artifact. Generated if `unity_cloud_build_download_binary` is true
- `ARTIFACT_FILENAME` just the filename of the Unity Cloud Build artifact. Generated if `unity_cloud_build_download_binary` is true
- `SHARE_URL` url to sharing url. Generated if `unity_cloud_build_create_share` is true


Running Action Locally
================================
On Macos;
- ~`python3 pip install poetry`~
- `pip3 install poetry`
- `poetry install`
- `export GITHUB_WORKSPACE=./Workspace`
- Build main on Android target/configuration
	- `poetry run python -m action --api_key=fffff --org_id=YOURORGID --project_id=PROJECT_ID --primary_build_target=Android --github_branch_ref=refs/head/main`
- List all projects & their build targets
	- `poetry run python -m action --api_key=fffff --org_id=YOURORGID`
- List build targets for a specific project
	- `poetry run python -m action --api_key=fffff --org_id=YOURORGID --project_id=PROJECT_ID`
- Download the artifact from an existing build
	- `poetry run python -m action --api_key=fffff --org_id=YOURORGID --project_id=PROJECT_ID --build_target_id=Android --existing_build_number=123 --download_binary=true`
- Get a share url from an existing build
	- `poetry run python -m action --api_key=fffff --org_id=YOURORGID --project_id=PROJECT_ID --build_target_id=Android --existing_build_number=123 --create_share_url=true`
- Start a build on another branch; assuming the base target isn't configured to it already
	- `poetry run python -m action --api_key=fffff --org_id=YOURORGID --project_id=PROJECT_ID --primary_build_target=Android --github_branch_ref=MyTestBranch`


Uploading to AppStoreConnect TestFlight
===================================
Uploading ios `.ipa` builds (`todo: how to configure build to generate .ipa`) to testflight is a simple process via `xcode`'s command line tools. 
This can be implemented in a workflow with just a single step
- The step needs to run on a macos runner (eg. `macos-latest`)
```
   - name: Upload .ipa to TestFlight
     run: xcrun altool --upload-app --file ${IOS_IPA_FILENAME} --type ios --bundle-id ${{ env.IosBundleId }} --bundle-version ${{ env.IosBundleVersion }} --bundle-short-version-string ${{ env.IosBundleShortVersionString }} --apiKey ${APPSTORECONNECT_AUTH_KEY} --apiIssuer ${APPSTORECONNECT_AUTH_ISSUER}
```

- `IosBundleId` should be the bundle id in appstoreconnect, eg. `com.you.app` `todo: does it need to be correct, or is it taken from ipa?`
- `IosBundleVersion` can be 0. AppStoreConnect correctly uses the bundle version in the ipa
- `IosBundleShortVersionString` can be 0. AppStoreConnect correctly uses the bundle version in the ipa `todo: is this the build number?`
- Note; the `.p8` contents should be stored in `./private_keys/AuthKey_$APPSTORECONNECT_AUTH_KEY.p8` (same filename as downloaded in `keys` section of appstore connecct


Uploading to Oculus Development Release Channels
================================
This works. 
- `todo: document`


Uploading to UnityGameHosting (Unity Game Services)
=========================
This uses a CLI tool from unity 
- https://github.com/Unity-Technologies/unity-gaming-services-cli/releases/
Based on these docs
- https://services.docs.unity.com/guides/ugs-cli/latest/general/samples/ci-cd-pipeline-usage/github-actions/

You can download a mac exe for testing locally (To get CLI correct)

A service account (and it's `keyid` and `secret`)is required, with the following roles (for that project)
- Game Server Hosting Allocations Admin
- Game Server Hosting API Editor
- Game Server Hosting API Manager
- Game Server Hosting API Viewer
- Unity Environments Viewer
	- `gr: this one is NOT LISTED IN UNITY DOCS` very important!

Uploading to Steam
============================
- Generate `config.vdf` file with `steamcmd` https://developer.valvesoftware.com/wiki/SteamCMD
	- https://github.com/game-ci/steam-deploy#configuration
- Encode to base64 and put in a secret
	- `cat config/config.vdf | base64 > config.vdf.base64.txt`



Reusable Workflow
==================================
This repository contains a re-usable workflow; `./github/workflows/UnityCloudBuild.yml` which will
- Trigger this action to do a Build
- Share a Sharing Url to slack if slack channel & bot auth key are provided
- Upload to testflight with appstoreconnect credentials
- Upload to oculus dev release channels
