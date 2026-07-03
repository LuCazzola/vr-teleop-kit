FROM ubuntu:22.04

ARG USERNAME=USERNAME
ARG USER_UID=1000
ARG USER_GID=$USER_UID

# Fix frontend warning during apt installs
ENV DEBIAN_FRONTEND=noninteractive

# Delete user if it exists in container (e.g Ubuntu Noble: ubuntu)
RUN if id -u $USER_UID ; then userdel `id -un $USER_UID` ; fi

# Create the user
RUN groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID -m $USERNAME \
    && usermod -aG plugdev,video $USERNAME \
    #
    # Add sudo support and tools
    && apt-get update \
    && apt-get install -y sudo curl \
    && echo $USERNAME ALL=\(root\) NOPASSWD:ALL > /etc/sudoers.d/$USERNAME \
    && chmod 0440 /etc/sudoers.d/$USERNAME \
    # Additional os tools
    && apt-get install -y android-tools-adb libgl1-mesa-glx libglx-mesa0 libgl1 mesa-utils scrcpy \
    # Clean up apt caches to keep the image slim
    && rm -rf /var/lib/apt/lists/*
    
RUN apt-get update && apt-get upgrade -y && apt-get install -y python3-pip && rm -rf /var/lib/apt/lists/*
ENV SHELL=/bin/bash

# Install uv globally
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute

USER $USERNAME
CMD ["/bin/bash"]