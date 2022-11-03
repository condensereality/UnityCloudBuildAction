# Run Unity Cloud Build Action

This actions allows us to run Unity Cloud Build jobs from GitHub on pull request and merge to main branch.
The action uses the Unity Cloud Build REST API to kick off a build and then polls it until the job is complete.
If the job fails, the action will return an exit code 1. If it succeeds it will return an exit code 0.

For pull requests, the action will create a new build target in Unity Cloud Build first before kicking off a build.
We have to do this because Unity Cloud Build does not support overridding the branch name at runtime. So if we didn't
do this, all the build would be run against the main branch and not the changes in the PR branch - not ideal for a pull request.

If a PR has further changes to pushed to it after initially being opened, the action will re-use the previous setup PR build target.
Doing this allows us to make use of Unity Cloud Build's build target caching mechanism which speeds up subsequent builds slightly.

## Inputs

The action requires a series of inputs from the workflow file. These are then passed to the action as environment variables that the
action then picks up and utilises to make relevant calls to Unity Cloud Build.

### `unity_cloud_build_api_key`

**Required**  - The Unity Cloud Build API key - this is best stored as a github secret.

### `unity_cloud_build_org_id`

**Required**  - The Unity Cloud Build organisation ID - this is can be found by [browsing your organisations](https://id.unity.com/en/organizations/) and finding the organisation name in any links/urls. (expected to be a string)

### `unity_cloud_build_project_id`

**Required**  - The Unity Cloud Build project ID - As with organisation ID, find this in urls for projects in your Unity Cloud Dashboard. (expected to be a string)

### `unity_cloud_build_primary_target`

**Required**  - The Unity Cloud Build primary build target - this is build target name. (Find this via urls, expected to be without spaces)

### `unity_cloud_build_target_platform`

**Required**  - The Unity Cloud Build target platform - this is target build platform - currently either ``ios``, ``android`` or ``webgl``.

### `unity_cloud_build_polling_interval`

**Optional**  - The frequency with which to query Unity Cloud Build jobs - the default is ``60`` seconds.

### `unity_cloud_build_download_binary`

**Optional** - Whether or not to download the built binary to your ``GITHUB_WORKSPACE``

## Example usage

Please find an example usage below for reference.

```yaml
jobs:
  run-unity-cloud-build-condense-live-android:
    name: Run Unity Cloud Build - Condense Live (Android)
    runs-on: ubuntu-22.04
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
        id: run-unity-cloud-build-action
        with:
          unity_cloud_build_api_key: ${{ secrets.UNITY_CLOUD_BUILD_API_KEY }}
          unity_cloud_build_org_id: yourorganisation
          unity_cloud_build_project_id: yourgame
          unity_cloud_build_polling_interval: 60
          unity_cloud_build_primary_target: yourgame-ios-default
          unity_cloud_build_target_platform: android
          unity_cloud_build_download_binary: false
```
