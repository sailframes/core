#!/bin/bash
#
# SailFrames AWS Deployment Script
#
# Prerequisites:
#   - AWS CLI configured with appropriate permissions
#   - Domain sailframes.com with Cloudflare DNS
#
# Usage:
#   ./deploy.sh [prod|staging]
#

set -e

# Configuration
ENVIRONMENT="${1:-prod}"
STACK_NAME="sailframes-${ENVIRONMENT}"
REGION="${AWS_REGION:-us-east-1}"
DOMAIN="sailframes.com"

# Paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LAMBDA_DIR="$PROJECT_ROOT/lambda"
WEB_DIR="$PROJECT_ROOT/web"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# Check prerequisites
check_prerequisites() {
    log "Checking prerequisites..."

    if ! command -v aws &> /dev/null; then
        error "AWS CLI not installed"
    fi

    if ! aws sts get-caller-identity &> /dev/null; then
        error "AWS credentials not configured"
    fi

    if ! command -v zip &> /dev/null; then
        error "zip command not found"
    fi

    log "Prerequisites OK"
}

# Create Lambda code bucket if needed
create_lambda_bucket() {
    local bucket="sailframes-lambda-code-${ENVIRONMENT}"

    echo "Checking Lambda code bucket: $bucket" >&2

    if ! aws s3 ls "s3://$bucket" &> /dev/null; then
        echo "Creating Lambda code bucket..." >&2
        aws s3 mb "s3://$bucket" --region "$REGION" >&2

        # Block public access
        aws s3api put-public-access-block \
            --bucket "$bucket" \
            --public-access-block-configuration \
            "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" >&2
    fi

    echo "$bucket"
}

# Package and upload Lambda functions
package_lambdas() {
    local bucket="$1"

    log "Packaging Lambda functions..."

    local functions=("process_upload" "api_sessions" "api_data" "api_video" "api_analysis")

    for func in "${functions[@]}"; do
        local func_dir="$LAMBDA_DIR/$func"
        local zip_file="/tmp/${func}.zip"

        if [ -d "$func_dir" ]; then
            log "  Packaging $func..."

            # Create zip
            (cd "$func_dir" && zip -r "$zip_file" .)

            # Upload to S3
            aws s3 cp "$zip_file" "s3://$bucket/${func}.zip"

            rm -f "$zip_file"
        else
            warn "  Lambda directory not found: $func_dir"
        fi
    done

    log "Lambda functions uploaded"
}

# Deploy CloudFormation stack
deploy_stack() {
    log "Deploying CloudFormation stack: $STACK_NAME"

    local template="$SCRIPT_DIR/cloudformation.yaml"

    if [ ! -f "$template" ]; then
        error "CloudFormation template not found: $template"
    fi

    # Check if stack exists
    if aws cloudformation describe-stacks --stack-name "$STACK_NAME" &> /dev/null; then
        log "Updating existing stack..."
        aws cloudformation update-stack \
            --stack-name "$STACK_NAME" \
            --template-body "file://$template" \
            --parameters \
                ParameterKey=Environment,ParameterValue="$ENVIRONMENT" \
            --capabilities CAPABILITY_NAMED_IAM \
            --region "$REGION" 2>&1 || {
                local exit_code=$?
                # Check if "No updates" message
                if aws cloudformation describe-stacks --stack-name "$STACK_NAME" &> /dev/null; then
                    log "Stack exists, may have no updates needed"
                    return 0
                fi
                return $exit_code
            }

        log "Waiting for stack update..."
        aws cloudformation wait stack-update-complete \
            --stack-name "$STACK_NAME" \
            --region "$REGION" 2>&1 || true
    else
        log "Creating new stack..."
        aws cloudformation create-stack \
            --stack-name "$STACK_NAME" \
            --template-body "file://$template" \
            --parameters \
                ParameterKey=Environment,ParameterValue="$ENVIRONMENT" \
            --capabilities CAPABILITY_NAMED_IAM \
            --region "$REGION"

        log "Waiting for stack creation (this may take 5-10 minutes)..."
        aws cloudformation wait stack-create-complete \
            --stack-name "$STACK_NAME" \
            --region "$REGION"
    fi

    log "Stack deployment complete"
}

# Deploy static website
deploy_website() {
    log "Deploying website..."

    # Get website bucket from stack outputs
    local website_bucket
    website_bucket=$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --query "Stacks[0].Outputs[?OutputKey=='WebsiteBucketName'].OutputValue" \
        --output text \
        --region "$REGION")

    if [ -z "$website_bucket" ] || [ "$website_bucket" = "None" ]; then
        error "Could not get website bucket from stack outputs"
    fi

    # Get API endpoint
    local api_endpoint
    api_endpoint=$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" \
        --output text \
        --region "$REGION")

    log "Uploading to: $website_bucket"
    log "API endpoint: $api_endpoint"

    # Create config.js with API URL
    echo "window.SAILFRAMES_API_URL = '${api_endpoint}';" > "$WEB_DIR/config.js"

    # Sync website files
    aws s3 sync "$WEB_DIR" "s3://$website_bucket/" \
        --delete \
        --cache-control "max-age=31536000" \
        --exclude "*.html" \
        --exclude "config.js" \
        --region "$REGION"

    # Upload HTML and config with shorter cache
    aws s3 sync "$WEB_DIR" "s3://$website_bucket/" \
        --exclude "*" \
        --include "*.html" \
        --include "config.js" \
        --cache-control "max-age=60" \
        --region "$REGION"

    log "Website deployed"
}

# Print stack outputs
print_outputs() {
    log "Stack outputs:"

    aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" \
        --output table \
        --region "$REGION"

    echo ""

    local website_url
    website_url=$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --query "Stacks[0].Outputs[?OutputKey=='WebsiteURL'].OutputValue" \
        --output text \
        --region "$REGION")

    local api_endpoint
    api_endpoint=$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" \
        --output text \
        --region "$REGION")

    echo "========================================"
    echo " Access your site:"
    echo "========================================"
    echo ""
    echo "  Website: $website_url"
    echo "  API:     $api_endpoint"
    echo ""
    echo "To set up Cloudflare with CloudFront later, run:"
    echo "  aws acm request-certificate --domain-name sailframes.com --validation-method DNS"
    echo ""
}

# Main
main() {
    echo ""
    echo "========================================="
    echo " SailFrames AWS Deployment"
    echo " Environment: $ENVIRONMENT"
    echo " Region: $REGION"
    echo "========================================="
    echo ""

    check_prerequisites

    local lambda_bucket
    lambda_bucket=$(create_lambda_bucket)

    package_lambdas "$lambda_bucket"
    deploy_stack
    deploy_website
    print_outputs

    echo ""
    log "Deployment complete!"
    echo ""
}

main
