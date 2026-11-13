# Independent CUDA service. FoundationPose source is supplied as a build context;
# weights and the object mesh are mounted read-only at runtime.
ARG POSE6D_BASE_IMAGE=docker.m.daocloud.io/pytorch/pytorch:2.6.0-cuda12.4-cudnn9-devel@sha256:0cf3402e946b7c384ba943ee05c90b4c5a4a05227923921f2b0918c011cfaf56
FROM ${POSE6D_BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/pose6d/src:/opt/pose6d/reference \
    XDG_CACHE_HOME=/tmp/cache MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /opt/pose6d/reference
RUN apt-get update && apt-get install -y --no-install-recommends \
    git cmake build-essential libeigen3-dev libboost-system-dev libboost-program-options-dev \
    libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
COPY config/python/pose6d-requirements.txt /tmp/pose6d-requirements.txt
RUN python3 -m pip install --no-cache-dir -r /tmp/pose6d-requirements.txt
# Docker builds do not expose a GPU for PyTorch to autodetect target architectures.
ENV TORCH_CUDA_ARCH_LIST="8.6;8.9" FORCE_CUDA=1 MAX_JOBS=2
ARG POSE6D_USE_PREINSTALLED_PYTORCH3D=0
RUN if [ "$POSE6D_USE_PREINSTALLED_PYTORCH3D" = 1 ]; then \
      python3 -c 'import torch, pytorch3d._C'; \
    else \
      python3 -m pip install --no-cache-dir --no-build-isolation \
        'git+https://github.com/facebookresearch/pytorch3d.git@204fe5831b3a5bbe079f09d72afb4515178f8892'; \
    fi
RUN --mount=type=bind,from=nvdiffrast_source,target=/tmp/nvdiffrast-source \
    cp -a /tmp/nvdiffrast-source /tmp/nvdiffrast-build \
    && rm -rf /tmp/nvdiffrast-build/build /tmp/nvdiffrast-build/.git \
    && python3 -m pip install --no-cache-dir --no-build-isolation /tmp/nvdiffrast-build \
    && rm -rf /tmp/nvdiffrast-build
COPY --from=foundation_reference /Utils.py /datareader.py /estimater.py /opt/pose6d/reference/
COPY --from=foundation_reference /learning /opt/pose6d/reference/learning
COPY --from=foundation_reference /mycpp/CMakeLists.txt /opt/pose6d/reference/mycpp/CMakeLists.txt
COPY --from=foundation_reference /mycpp/include /opt/pose6d/reference/mycpp/include
COPY --from=foundation_reference /mycpp/src /opt/pose6d/reference/mycpp/src
RUN cmake -S mycpp -B mycpp/build -DCMAKE_BUILD_TYPE=Release \
    -Dpybind11_DIR="$(python3 -m pybind11 --cmakedir)" \
    && cmake --build mycpp/build -j2
# Some base images contain an unmanaged cv2 extension which masks the pip wheel.
RUN cv2_dir="$(python3 -c 'import importlib.util; print(next(iter(importlib.util.find_spec("cv2").submodule_search_locations)))')" \
    && if [ -d "$cv2_dir/python-3.10" ]; then rm -rf "$cv2_dir/python-3.10" "$cv2_dir/config-3.10.py"; fi \
    && python3 -c 'import cv2, numpy; print("opencv", cv2.__version__, "numpy", numpy.__version__)'
COPY src/grasp/grasp_protocol /opt/pose6d/src/grasp_protocol
COPY src/pose6d/pose6d /opt/pose6d/src/pose6d
EXPOSE 8092
ENTRYPOINT ["python3", "-m", "pose6d.server"]
CMD ["--host", "0.0.0.0", "--mesh", "/models/eu_plug/TexturedMeshSimplified.obj"]
