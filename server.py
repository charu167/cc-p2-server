import threading
from flask import Flask, request
import boto3
from botocore.config import Config
from dotenv import load_dotenv
import os
import time
import uuid
import json
from collections import defaultdict
import logging

logging.basicConfig(
    filename="pending_requests.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

# Load environment variables
load_dotenv()
aws_access_key_id = os.getenv("aws_access_key_id")
aws_secret_access_key = os.getenv("aws_secret_access_key")
region_name = os.getenv("region_name")
input_bucket = os.getenv("input_bucket")
input_queue_url = os.getenv("input_queue_url")
output_queue_url = os.getenv("output_queue_url")


class SQS:
    def __init__(self):
        self.sqs_client = boto3.client(
            "sqs",
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region_name,
            config=Config(
                connect_timeout=5, read_timeout=5, retries={"max_attempts": 0}
            ),
        )

    def recieve_message(self, queue_url, wait_time=3):
        data = self.sqs_client.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=wait_time
        )
        return data

    def push_message(self, queue_url, message_body):
        data = self.sqs_client.send_message(
            QueueUrl=queue_url, MessageBody=json.dumps(message_body)
        )
        return data

    def block_mesage(self, queue_url, receipt_handle, time=10):
        return self.sqs_client.change_message_visibility(
            QueueUrl=queue_url, ReceiptHandle=receipt_handle, VisibilityTimeout=time
        )

    def delete_message(self, queue_url, receipt_handle):
        data = self.sqs_client.delete_message(
            QueueUrl=queue_url, ReceiptHandle=receipt_handle
        )
        return data


class S3:
    def __init__(self):
        self.s3_client = boto3.client(
            "s3",
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=region_name,
            config=Config(
                connect_timeout=5, read_timeout=5, retries={"max_attempts": 0}
            ),
        )

    def download_file(self, bucket_name, s3_file_path, local_file_path):
        return self.s3_client.download_file(
            Bucket=bucket_name, Key=s3_file_path, Filename=local_file_path
        )

    def upload_file(self, file_data, bucket_name, s3_file_key):
        s3_response = self.s3_client.put_object(
            Body=file_data, Bucket=bucket_name, Key=s3_file_key
        )
        return s3_response


# Initialize Flask app
app = Flask(__name__)

# Global hashmap for responses
resp_lock = threading.Lock()
Responses = defaultdict(str)

# Global pending requests number
pending_requests = 0
pending_requests_lock = threading.Lock()

# Initialize S3 and SQS
s3 = S3()
sqs = SQS()


def poll_response_queue():
    global Responses
    while True:
        try:
            data = sqs.recieve_message(output_queue_url, wait_time=3)

            if "Messages" in data:
                message = data["Messages"][0]
                output_data = json.loads(message["Body"])
                receipt_handle = message["ReceiptHandle"]
                req_id = output_data["req_id"]
                result = output_data["result"]

                with resp_lock:
                    Responses[req_id] = result

                sqs.delete_message(output_queue_url, receipt_handle)

        except Exception as e:
            print(f"Polling thread crashed with exception: {e}", flush=True)
            time.sleep(2)


@app.route("/", methods=["POST"])
def home():
    global pending_requests

    with pending_requests_lock:
        pending_requests += 1

    # Extract info from request
    file = request.files["inputFile"]
    s3_file_key = file.filename
    file_data = file.read()

    # Upload file to S3
    s3_response = s3.upload_file(file_data, input_bucket, s3_file_key)
    if s3_response["ResponseMetadata"]["HTTPStatusCode"] == 200:

        req_id = str(uuid.uuid4())
        message = {"file_key": s3_file_key, "req_id": req_id}

        # Send message to input SQS queue
        sqs_response = sqs.push_message(input_queue_url, message)
        if sqs_response["ResponseMetadata"]["HTTPStatusCode"] == 200:

            while True:
                time.sleep(0.2)
                with resp_lock:
                    if req_id in Responses:
                        result = Responses.pop(req_id)
                        suffix = os.path.splitext(s3_file_key)[0]
                        response = f"{suffix}:{result}"
                        with pending_requests_lock:
                            pending_requests -= 1
                        return response


def pending_requests_logger():
    global pending_requests

    while True:
        with pending_requests_lock:
            logging.info(f"Pending requests: {pending_requests}")

        time.sleep(1)


# Start polling thread and Flask app
if __name__ == "__main__":
    polling_thread = threading.Thread(
        target=poll_response_queue, name="SQS-Poller", daemon=True
    )
    polling_thread.start()

    # pending_requests_logger_thread = threading.Thread(
    #     target=pending_requests_logger, name='PendingRequestLogger', daemon=True
    # )
    # pending_requests_logger_thread.start()

    app.run(debug=True, port=8000, use_reloader=False, host="0.0.0.0")
