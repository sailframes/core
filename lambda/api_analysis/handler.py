"""
SailFrames API - Get Session Analysis
Returns pre-computed analysis results for a session.
"""

import json
import os
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
DATA_BUCKET = os.environ.get('DATA_BUCKET', 'sailframes-data-prod')


def lambda_handler(event, context):
    """Return analysis results for a session."""
    try:
        path_params = event.get('pathParameters', {})
        device_id = path_params.get('device_id')
        date = path_params.get('date')

        if not device_id or not date:
            return error_response(400, 'Missing device_id or date')

        # Load analysis.json
        key = f"processed/{device_id}/{date}/analysis.json"
        try:
            response = s3.get_object(Bucket=DATA_BUCKET, Key=key)
            analysis = json.loads(response['Body'].read().decode('utf-8'))
        except s3.exceptions.NoSuchKey:
            return error_response(404, 'Analysis not found. Run processing first.')
        except Exception as e:
            logger.error(f"Error loading analysis: {e}")
            return error_response(500, f"Failed to load analysis: {e}")

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps(analysis)
        }
    except Exception as e:
        logger.error(f"Error: {e}")
        return error_response(500, str(e))


def error_response(status: int, message: str) -> dict:
    return {
        'statusCode': status,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps({'message': message})
    }
