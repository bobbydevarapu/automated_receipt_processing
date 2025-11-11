import json
import os
import boto3
import uuid
from datetime import datetime
import urllib.parse
import logging

# Logging setup
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment variables
DYNAMODB_TABLE = os.environ.get('DYNAMODB_TABLE', 'Receipts')
SES_SENDER_EMAIL = os.environ.get('SES_SENDER_EMAIL')
SES_RECIPIENT_EMAIL = os.environ.get('SES_RECIPIENT_EMAIL')
SES_REGION = os.environ.get('SES_REGION', 'us-east-1')

# AWS clients
s3 = boto3.client('s3')
textract = boto3.client('textract')
dynamodb = boto3.resource('dynamodb')
ses = boto3.client('ses', region_name=SES_REGION)

def lambda_handler(event, context):
    try:
        record = event['Records'][0]
        bucket = record['s3']['bucket']['name']
        key = urllib.parse.unquote_plus(record['s3']['object']['key'])

        logger.info(f"Processing file: s3://{bucket}/{key}")

        s3.head_object(Bucket=bucket, Key=key)

        # 1) Process receipt with Textract
        receipt_data = process_receipt_with_textract(bucket, key)

        # 2) Store in DynamoDB
        store_receipt_in_dynamodb(receipt_data)

        # 3) Send email via SES
        send_email_notification(receipt_data)

        logger.info("Processing complete")
        return {"statusCode": 200, "body": json.dumps("Receipt processed successfully!")}

    except Exception as e:
        logger.exception("Error processing receipt")
        return {"statusCode": 500, "body": json.dumps(f"Error: {str(e)}")}


def process_receipt_with_textract(bucket, key):
    """Use Textract AnalyzeExpense to extract receipt data"""
    response = textract.analyze_expense(
        Document={'S3Object': {'Bucket': bucket, 'Name': key}}
    )

    receipt_id = str(uuid.uuid4())
    now = datetime.now().strftime('%Y-%m-%d')

    receipt_data = {
        'receipt_id': receipt_id,
        'date': now,
        'vendor': 'Unknown',
        'total': '0.00',
        'items': [],
        's3_path': f"s3://{bucket}/{key}"
    }

    expense_docs = response.get('ExpenseDocuments', [])
    if not expense_docs:
        logger.warning("No ExpenseDocuments found")
        return receipt_data

    doc = expense_docs[0]

    for field in doc.get('SummaryFields', []):
        field_type = field.get('Type', {}).get('Text', '')
        value = field.get('ValueDetection', {}).get('Text', '')
        if field_type == 'TOTAL':
            receipt_data['total'] = value
        elif field_type in ('INVOICE_RECEIPT_DATE', 'DATE'):
            receipt_data['date'] = value
        elif field_type in ('VENDOR_NAME', 'SUPPLIER_NAME'):
            receipt_data['vendor'] = value

    for group in doc.get('LineItemGroups', []):
        for line_item in group.get('LineItems', []):
            item = {}
            for f in line_item.get('LineItemExpenseFields', []):
                f_type = f.get('Type', {}).get('Text', '')
                val = f.get('ValueDetection', {}).get('Text', '')
                if f_type == 'ITEM':
                    item['name'] = val
                elif f_type == 'PRICE':
                    item['price'] = val
                elif f_type == 'QUANTITY':
                    item['quantity'] = val
            if 'name' in item:
                item.setdefault('price', '0.00')
                item.setdefault('quantity', '1')
                receipt_data['items'].append(item)

    logger.info(f"Extracted data: {json.dumps(receipt_data)}")
    return receipt_data


def store_receipt_in_dynamodb(receipt_data):
    """Store extracted data in DynamoDB"""
    table = dynamodb.Table(DYNAMODB_TABLE)
    table.put_item(Item={
        'receipt_id': receipt_data['receipt_id'],
        'date': receipt_data['date'],
        'vendor': receipt_data['vendor'],
        'total': receipt_data['total'],
        'items': receipt_data['items'],
        's3_path': receipt_data['s3_path'],
        'processed_timestamp': datetime.now().isoformat()
    })
    logger.info("Data stored in DynamoDB")


def send_email_notification(receipt_data):
    """Send summary email via SES"""
    items_html = "".join(
        f"<li>{i.get('name','Unknown')} - ${i.get('price','0.00')} × {i.get('quantity','1')}</li>"
        for i in receipt_data.get('items', [])
    ) or "<li>No items detected</li>"

    html_body = f"""
    <html><body>
      <h2>Receipt Processed</h2>
      <p><strong>Vendor:</strong> {receipt_data['vendor']}</p>
      <p><strong>Date:</strong> {receipt_data['date']}</p>
      <p><strong>Total:</strong> ${receipt_data['total']}</p>
      <p><strong>Receipt ID:</strong> {receipt_data['receipt_id']}</p>
      <p><strong>S3 Path:</strong> {receipt_data['s3_path']}</p>
      <h3>Items</h3>
      <ul>{items_html}</ul>
    </body></html>
    """

    ses.send_email(
        Source=SES_SENDER_EMAIL,
        Destination={'ToAddresses': [SES_RECIPIENT_EMAIL]},
        Message={
            'Subject': {'Data': f"Receipt Processed - {receipt_data['vendor']} ${receipt_data['total']}"},
            'Body': {'Html': {'Data': html_body}}
        }
    )
    logger.info(f"Email sent to {SES_RECIPIENT_EMAIL}")