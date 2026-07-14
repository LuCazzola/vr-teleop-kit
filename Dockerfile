FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

###
### User Setup
###

ARG USERNAME=dev
# Overridden from .env via docker-compose to match host uid/gid, so `dev`
# can write to the bind-mounted /workspace.
ARG USER_UID=1000
ARG USER_GID=$USER_UID

# Delete user if it exists in container (e.g Ubuntu Noble: ubuntu)
RUN if id -u $USER_UID ; then userdel `id -un $USER_UID` ; fi

# Create the user
RUN groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID -m $USERNAME \
    && usermod -aG plugdev,video $USERNAME \
    # Add sudo support and tools
    && apt-get update \
    && apt-get install -y sudo curl \
    && echo $USERNAME ALL=\(root\) NOPASSWD:ALL > /etc/sudoers.d/$USERNAME \
    && chmod 0440 /etc/sudoers.d/$USERNAME

RUN apt-get update && apt-get upgrade -y && apt-get install -y python3-pip && rm -rf /var/lib/apt/lists/*
ENV SHELL=/bin/bash


###
### Additional OS-side Tools
###

RUN apt-get update && apt-get install -y \
    android-tools-adb \
    libgl1-mesa-glx \
    libglx-mesa0 \
    libgl1 \
    mesa-utils \
    scrcpy \
    && rm -rf /var/lib/apt/lists/*

###
### Virtual Environment Setup (for Python dependencies)
###

# Install uv globally
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

###
### Close out the container
###

WORKDIR /workspace

USER $USERNAME

ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute

# Venv lives in $HOME, not /workspace: WORKDIR is root-owned, and
# /workspace gets bind-mounted over at runtime anyway.
ENV UV_PROJECT_ENVIRONMENT=/home/$USERNAME/.venv
ENV PATH=/home/$USERNAME/.venv/bin:$PATH

# README.md/src required: hatchling builds this package itself (not just
# its deps) and needs the readme file to exist on disk.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# av/aiortc/fastapi etc. (needed by vr-teleop-relay) are in the optional
# "relay" group, not the base deps.
RUN uv sync --frozen --extra relay \
    && echo "source $UV_PROJECT_ENVIRONMENT/bin/activate" >> ~/.bashrc

CMD ["/bin/bash"]
