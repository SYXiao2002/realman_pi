# Independent opt-in inference image: no ROS 2 or hardware access.
# Mirror follows the robot image's domestic default; override GRASP_BASE_IMAGE
# for an upstream/internal registry. Digest pins the inspected image contents.
ARG GRASP_BASE_IMAGE=docker.m.daocloud.io/pytorch/pytorch:2.7.1-cuda12.6-cudnn9-runtime@sha256:2b59b1b91885677814f78be1f8df48a25d5dc952eb6580eaecfefca510f9afd3
FROM ${GRASP_BASE_IMAGE}
ARG UBUNTU_APT_MIRROR=https://mirrors.aliyun.com/ubuntu
ARG PIP_INDEX_URL=https://pypi.mirrors.ustc.edu.cn/simple
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/grasp/src/grasp \
    YOLO_CONFIG_DIR=/tmp/ultralytics MPLCONFIGDIR=/tmp/matplotlib \
    XDG_CACHE_HOME=/tmp/cache
WORKDIR /opt/grasp
# MobileSAM's pinned Git dependency needs git at image build time. GL libraries
# satisfy OpenCV's wheel without requiring an X server or display mapping.
RUN sed -i -e "s|http://archive.ubuntu.com/ubuntu|${UBUNTU_APT_MIRROR}|g" \
    -e "s|http://security.ubuntu.com/ubuntu|${UBUNTU_APT_MIRROR}|g" /etc/apt/sources.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
COPY config/python/grasp-server-requirements.txt /tmp/grasp-server-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/grasp-server-requirements.txt
RUN python -c "import torch,numpy,cv2; assert torch.__version__.split('+')[0] == '2.7.1'; assert torch.version.cuda == '12.6'; assert numpy.__version__ == '1.26.4'"
COPY src/grasp/grasp_protocol /opt/grasp/src/grasp/grasp_protocol
COPY src/grasp/grasp_server /opt/grasp/src/grasp/grasp_server
COPY config/grasp /opt/grasp/config/grasp
# Arbitrary UID is allowed; only /tmp and the explicit logs mount are writable.
USER 1000:1000
EXPOSE 8091
ENTRYPOINT ["python", "-m", "grasp_server.server"]
CMD ["--config", "/opt/grasp/config/grasp/server.yaml"]
