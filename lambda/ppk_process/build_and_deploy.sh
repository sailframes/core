#!/bin/bash
# Build and deploy PPK Processing Lambda container

set -e

REGION=${AWS_REGION:-us-east-1}
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/sailframes-ppk-process"
LAMBDA_NAME="sailframes-ppk-process"

echo "Building PPK Processing Lambda container..."
cd "$(dirname "$0")"

# Login to ECR
aws ecr get-login-password --region ${REGION} | docker login --username AWS --password-stdin ${ECR_REPO}

# Build container
docker build --platform linux/amd64 -t sailframes-ppk-process .

# Tag and push
docker tag sailframes-ppk-process:latest ${ECR_REPO}:latest
docker push ${ECR_REPO}:latest

# Check if Lambda exists
if aws lambda get-function --function-name ${LAMBDA_NAME} --region ${REGION} 2>/dev/null; then
    echo "Updating existing Lambda..."
    aws lambda update-function-code \
        --function-name ${LAMBDA_NAME} \
        --image-uri ${ECR_REPO}:latest \
        --region ${REGION}
else
    echo "Creating new Lambda..."
    aws lambda create-function \
        --function-name ${LAMBDA_NAME} \
        --package-type Image \
        --code ImageUri=${ECR_REPO}:latest \
        --role arn:aws:iam::${ACCOUNT_ID}:role/sailframes-lambda-prod \
        --timeout 600 \
        --memory-size 1024 \
        --environment "Variables={DATA_BUCKET=sailframes-fleet-data-prod}" \
        --region ${REGION}
fi

echo "Done! Lambda deployed: ${LAMBDA_NAME}"
