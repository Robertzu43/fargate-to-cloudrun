import boto3
sqs = boto3.client("sqs")
while True:
    for m in sqs.receive_message(QueueUrl="https://sqs.us-east-1.amazonaws.com/123456789012/jobs").get("Messages", []):
        sqs.delete_message(QueueUrl="https://sqs.us-east-1.amazonaws.com/123456789012/jobs", ReceiptHandle=m["ReceiptHandle"])
