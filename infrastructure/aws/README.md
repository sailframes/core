# SailFrames AWS Infrastructure

Terraform configuration for the E1 fleet data upload system.

## Architecture

```
E1 Device → WiFi → API Gateway → Lambda → S3
                                    ↓
                              CloudWatch Logs
```

## Components

- **S3 Bucket**: Stores all uploaded fleet data
  - `raw/{boat_id}/{date}/{filename}` - Original uploads
  - Lifecycle: Standard → Standard-IA (90d) → Glacier (365d)

- **Lambda Function**: Receives uploads, stores in S3

- **API Gateway**: HTTP API endpoint for device uploads

## Deployment

### Prerequisites

1. AWS CLI configured with credentials
2. Terraform installed (v1.0+)

### Deploy

```bash
cd infrastructure/aws

# Initialize Terraform
terraform init

# Preview changes
terraform plan

# Deploy
terraform apply
```

### Get the API endpoint

```bash
terraform output api_endpoint
```

Copy this URL to your E1 config.txt:
```
upload_url=https://xxxxxxxxxx.execute-api.us-east-1.amazonaws.com/prod/upload
```

## S3 Data Structure

```
sailframes-fleet-data-prod/
├── raw/
│   ├── E1/
│   │   ├── 2026-03-31/
│   │   │   ├── E1_20260331_143022_nav.csv
│   │   │   ├── E1_20260331_143022_imu.csv
│   │   │   └── E1_20260331_143022.rtcm3
│   │   └── 2026-04-01/
│   │       └── ...
│   ├── E2/
│   │   └── ...
│   └── ...
└── processed/
    └── ... (PPK post-processed data)
```

## Monitoring

View upload logs:
```bash
aws logs tail /aws/apigateway/sailframes-upload --follow
```

View Lambda logs:
```bash
aws logs tail /aws/lambda/sailframes-upload-handler --follow
```

## Cost Estimate

For a 6-boat fleet with daily sailing:
- S3: ~$1-5/month (depending on data volume)
- Lambda: ~$0.50/month (minimal invocations)
- API Gateway: ~$1/month
- **Total: ~$5-10/month**

## Security Notes

- API is currently open (no authentication)
- For production, consider adding:
  - API key authentication
  - WAF rules
  - VPC endpoint for S3

To add API key auth, update the API Gateway configuration.
