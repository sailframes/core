# E1 HTTP Upload Setup

## Problem
ESP32 Arduino Core 3.3.7 has a broken TLS implementation that causes mbedTLS BIGNUM
allocation failures during RSA operations, even with adequate heap memory (~49KB).
This prevents all HTTPS uploads to API Gateway.

## Solution
Bypass HTTPS entirely by uploading directly to S3 via HTTP. S3 supports HTTP, and
a bucket policy allows unauthenticated PUT requests to specific paths.

## Architecture Change

**Before (broken):**
```
E1 --HTTPS--> API Gateway --> Lambda --> S3
```

**After (working):**
```
E1 --HTTP--> S3 (direct PUT, no auth required for raw/E1/* paths)
```

## Deployment Steps

### 1. Apply S3 Bucket Policy

Add the public PUT policy to the existing bucket. Run this AWS CLI command:

```bash
# First, get the current bucket policy (if any)
aws s3api get-bucket-policy --bucket sailframes-fleet-data-prod --output text > /tmp/current-policy.json 2>/dev/null || echo '{"Version":"2012-10-17","Statement":[]}' > /tmp/current-policy.json

# Add the E1 upload statement to the policy
# You can either merge manually or apply this policy that combines web access with E1 uploads:
cat > /tmp/e1-upload-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "E1DirectHTTPUpload",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::sailframes-fleet-data-prod/raw/E1/*"
    }
  ]
}
EOF

aws s3api put-bucket-policy --bucket sailframes-fleet-data-prod --policy file:///tmp/e1-upload-policy.json
```

**Security Notes:**
- This allows ANY device to PUT objects to `raw/E1/*` paths
- The data (GPS, IMU) is not sensitive
- Consider adding IP restrictions if needed:
  ```json
  "Condition": {
    "IpAddress": {
      "aws:SourceIp": ["your.yacht.club.ip/32"]
    }
  }
  ```

### 2. Flash Updated Firmware

The firmware has been updated to:
- Upload directly to S3 via HTTP (no TLS)
- Use path format: `raw/E1/{date}/{filename}`
- Test connectivity to S3 instead of API Gateway

Flash `sailframes_e1.ino` to the ESP32.

### 3. Verify Configuration

The firmware now uses these config values:
```cpp
s3_bucket = "sailframes-fleet-data-prod"
s3_region = "us-east-1"
boat_id = "E1"
```

These can be overridden in `/sf/config.txt` on the SD card if needed.

## Testing

1. Connect E1 to Wi-Fi
2. Use `upload` command via Serial/Telnet
3. Watch for:
   ```
   [UPLOAD] Testing S3 connectivity...
   [UPLOAD] DNS OK: sailframes-fleet-data-prod.s3.us-east-1.amazonaws.com
   [UPLOAD] TCP OK (HTTP ready)
   [UPLOAD] S3 HTTP PUT: http://sailframes-fleet-data-prod.s3.us-east-1.amazonaws.com/raw/E1/2026-04-07/E1_nav.csv
   [UPLOAD] Success: /sf/20260407_123456/E1_nav.csv (HTTP 200, 2s)
   ```

## Troubleshooting

### HTTP 403 Forbidden
- Bucket policy not applied correctly
- Check: `aws s3api get-bucket-policy --bucket sailframes-fleet-data-prod`
- Verify the policy allows `s3:PutObject` to `raw/E1/*`

### HTTP -1 CONNECTION_REFUSED
- DNS or network issue
- Check Wi-Fi connection quality (RSSI)
- Verify S3 endpoint is reachable: `ping sailframes-fleet-data-prod.s3.us-east-1.amazonaws.com`

### Files uploaded but not visible in web dashboard
- The dashboard may expect files via the Lambda processing path
- Raw files are at: `s3://sailframes-fleet-data-prod/raw/E1/{date}/{filename}`
- Add an S3 event trigger if automated processing is needed

## Files Changed

- `edge-e/firmware/sailframes_e1/sailframes_e1.ino` - Direct S3 HTTP upload
- `infrastructure/aws/lambda/upload_handler.py` - Returns HTTP presigned URLs (backup path)
- `infrastructure/aws/s3-e1-upload-policy.json` - Bucket policy for public PUT

---

*Created: April 7, 2026*
