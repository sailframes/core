# SailFrames E1 Fleet Data Upload Infrastructure
# Terraform configuration for AWS S3 + Lambda + API Gateway
# Compatible with Terraform 0.12.x and AWS Provider 3.x

provider "aws" {
  region  = var.aws_region
  version = "~> 3.0"
}

variable "aws_region" {
  default = "us-east-1"
}

variable "environment" {
  default = "prod"
}

# S3 Bucket for fleet data
resource "aws_s3_bucket" "fleet_data" {
  bucket = "sailframes-fleet-data-${var.environment}"
  acl    = "private"

  versioning {
    enabled = true
  }

  lifecycle_rule {
    id      = "archive-old-data"
    enabled = true

    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 365
      storage_class = "GLACIER"
    }
  }

  tags = {
    Project = "SailFrames"
  }
}

# IAM Role for Lambda
resource "aws_iam_role" "lambda_role" {
  name = "sailframes-upload-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  name = "sailframes-upload-lambda-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.fleet_data.arn,
          "${aws_s3_bucket.fleet_data.arn}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

# Lambda function
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda/upload_handler.py"
  output_path = "${path.module}/lambda/upload_handler.zip"
}

resource "aws_lambda_function" "upload_handler" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "sailframes-upload-handler"
  role             = aws_iam_role.lambda_role.arn
  handler          = "upload_handler.lambda_handler"
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  runtime          = "python3.8"
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      BUCKET_NAME = aws_s3_bucket.fleet_data.id
    }
  }
}

# API Gateway (HTTP API v2)
resource "aws_apigatewayv2_api" "upload_api" {
  name          = "sailframes-upload-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["PUT", "POST", "GET"]
    allow_headers = ["*"]
  }
}

resource "aws_apigatewayv2_stage" "prod" {
  api_id      = aws_apigatewayv2_api.upload_api.id
  name        = "prod"
  auto_deploy = true
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id             = aws_apigatewayv2_api.upload_api.id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.upload_handler.invoke_arn
  integration_method = "POST"
}

resource "aws_apigatewayv2_route" "upload" {
  api_id    = aws_apigatewayv2_api.upload_api.id
  route_key = "PUT /upload"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "upload_post" {
  api_id    = aws_apigatewayv2_api.upload_api.id
  route_key = "POST /upload"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.upload_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.upload_api.execution_arn}/*/*"
}

# ============================================
# S3 EVENT NOTIFICATION FOR DATA PROCESSING
# ============================================
# Triggers the CloudFormation-deployed ProcessUploadFunction when
# CSV files are uploaded to the raw/ prefix

data "aws_lambda_function" "process_upload" {
  function_name = "sailframes-process-upload-${var.environment}"
}

resource "aws_lambda_permission" "s3_invoke_process_upload" {
  statement_id  = "AllowS3InvokeProcessUpload"
  action        = "lambda:InvokeFunction"
  function_name = data.aws_lambda_function.process_upload.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.fleet_data.arn
}

resource "aws_s3_bucket_notification" "raw_upload_trigger" {
  bucket = aws_s3_bucket.fleet_data.id

  # CSV files -> ProcessUpload Lambda (sensor data processing)
  lambda_function {
    lambda_function_arn = data.aws_lambda_function.process_upload.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "raw/"
    filter_suffix       = ".csv"
  }

  # RTCM3 files -> ProcessUpload Lambda (PPK data)
  lambda_function {
    lambda_function_arn = data.aws_lambda_function.process_upload.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "raw/"
    filter_suffix       = ".rtcm3"
  }

  # MP4 video files -> LinkVideos Lambda
  lambda_function {
    lambda_function_arn = data.aws_lambda_function.link_videos.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "raw/gopro/"
    filter_suffix       = ".MP4"
  }

  # LRV proxy video files -> LinkVideos Lambda
  lambda_function {
    lambda_function_arn = data.aws_lambda_function.link_videos.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "raw/gopro/"
    filter_suffix       = ".LRV"
  }

  depends_on = [
    aws_lambda_permission.s3_invoke_process_upload,
    aws_lambda_permission.s3_invoke_link_videos
  ]
}

# LinkVideos Lambda for GoPro video uploads
data "aws_lambda_function" "link_videos" {
  function_name = "sailframes-link-videos-${var.environment}"
}

resource "aws_lambda_permission" "s3_invoke_link_videos" {
  statement_id  = "AllowS3InvokeLinkVideos"
  action        = "lambda:InvokeFunction"
  function_name = data.aws_lambda_function.link_videos.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.fleet_data.arn
}

# Outputs
output "api_endpoint" {
  value       = "${aws_apigatewayv2_api.upload_api.api_endpoint}/prod/upload"
  description = "API Gateway endpoint URL - use this in config.txt upload_url"
}

output "s3_bucket" {
  value       = aws_s3_bucket.fleet_data.id
  description = "S3 bucket for fleet data"
}

output "process_upload_trigger" {
  value       = "Enabled: raw/*.csv -> ${data.aws_lambda_function.process_upload.function_name}"
  description = "S3 event notification for automatic data processing"
}

output "link_videos_trigger" {
  value       = "Enabled: raw/gopro/*.MP4, *.LRV -> ${data.aws_lambda_function.link_videos.function_name}"
  description = "S3 event notification for automatic video linking"
}
