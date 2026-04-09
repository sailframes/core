# SailFrames PPK Processing Lambda
# Container image with RTKLIB for PPK GNSS processing

FROM public.ecr.aws/lambda/python:3.11

# Install build dependencies
RUN yum update -y && \
    yum install -y \
        gcc \
        gcc-c++ \
        gcc-gfortran \
        make \
        git \
        gzip \
        tar \
    && yum clean all

# Clone and build RTKLIB
RUN cd /tmp && \
    git clone -b demo5 https://github.com/rtklibexplorer/RTKLIB.git && \
    cd RTKLIB/app/consapp && \
    cd convbin/gcc && \
    make && \
    cp convbin /opt/ && \
    cd ../../rnx2rtkp/gcc && \
    make && \
    cp rnx2rtkp /opt/ && \
    rm -rf /tmp/RTKLIB

# Create RTKLIB bin directory
RUN mkdir -p /opt/rtklib/bin && \
    mv /opt/convbin /opt/rtklib/bin/ && \
    mv /opt/rnx2rtkp /opt/rtklib/bin/

# Copy RTKLIB configuration file
COPY ppk.conf /opt/rtklib/ppk.conf

# Install Python dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy handler
COPY handler.py ${LAMBDA_TASK_ROOT}/

# Set environment variables
ENV CONVBIN_PATH=/opt/rtklib/bin/convbin
ENV RNX2RTKP_PATH=/opt/rtklib/bin/rnx2rtkp
ENV DATA_BUCKET=sailframes-fleet-data-prod

CMD ["handler.lambda_handler"]
