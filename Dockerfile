FROM python:3.10.4-alpine3.16

# note we allow to run as root and don't set a working directory
# as per the GitHub documentation here:
# https://docs.github.com/en/actions/creating-actions/dockerfile-support-for-github-actions

LABEL author="hello@condense.live"
LABEL maintainers="Condense Reality"

# copy and install action components and install action dependencies
COPY requirements.txt /requirements.txt
RUN pip install -r /requirements.txt
COPY entrypoint.sh /entrypoint.sh
COPY action.py /action.py

# set entrypoint for our action
ENTRYPOINT ["/entrypoint.sh"]
