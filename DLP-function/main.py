import google.cloud.dlp
from google.cloud import storage
from google.cloud import pubsub
from google.cloud import logging
import os

#  User-configurable Constants
PROJECT_ID = os.getenv('DLP_PROJECT_ID', 'banded-operator-370512')
STAGING_BUCKET = os.getenv('QUARANTINE_BUCKET', 'quar-1999')
SENSITIVE_BUCKET = os.getenv('SENSITIVE_DATA_BUCKET', 'sens-1999')
PUB_SUB_TOPIC = os.getenv('PUB_SUB_TOPIC', 'dlp-topic')
MIN_LIKELIHOOD = os.getenv('MIN_LIKELIHOOD', 'POSSIBLE')
MAX_FINDINGS = 0
INFO_TYPES = os.getenv('INFO_TYPES', 'FIRST_NAME,PHONE_NUMBER,EMAIL_ADDRESS,US_SOCIAL_SECURITY_NUMBER').split(',')
APP_LOG_NAME = os.getenv('LOG_NAME', 'DLP-classify-gcs-files')

# Initialize the Google Cloud client libraries
dlp = google.cloud.dlp_v2.DlpServiceClient()
storage_client = storage.Client()
publisher = pubsub.PublisherClient()
subscriber = pubsub.SubscriberClient()

LOG_SEVERITY_DEFAULT = 'DEFAULT'
LOG_SEVERITY_INFO = 'INFO'
LOG_SEVERITY_ERROR = 'ERROR'
LOG_SEVERITY_WARNING = 'WARNING'
LOG_SEVERITY_DEBUG = 'DEBUG'


def log(text, severity=LOG_SEVERITY_DEFAULT, log_name=APP_LOG_NAME):
    logging_client = logging.Client()
    logger = logging_client.logger(log_name)

    return logger.log_text(text, severity=severity)


def create_DLP_job(data, done):
    """This function is triggered by new files uploaded to the designated Cloud Storage quarantine/staging bucket.
         It creates a dlp job for the uploaded file.
      Arg:
         data: The Cloud Storage Event
      Returns:
          None. Debug information is printed to the log.
      """
    # Get the targeted file in the quarantine bucket
    file_name = data['name']
    log('Function triggered for file [{}] to start a DLP job of InfoTypes [{}]'.format(file_name, ','.join(INFO_TYPES)),
        severity=LOG_SEVERITY_INFO)

    # Prepare info_types by converting the list of strings (INFO_TYPES) into a list of dictionaries
    info_types = [{'name': info_type} for info_type in INFO_TYPES]

    # Convert the project id into a full resource id.
    parent = f"projects/{PROJECT_ID}"

    # Construct the configuration dictionary.
    inspect_job = {
        'inspect_config': {
            'info_types': info_types,
            'min_likelihood': MIN_LIKELIHOOD,
            'limits': {
                'max_findings_per_request': MAX_FINDINGS
            },
        },
        'storage_config': {
            'cloud_storage_options': {
                'file_set': {
                    'url':
                        'gs://{bucket_name}/{file_name}'.format(
                            bucket_name=STAGING_BUCKET, file_name=file_name)
                }
            }
        },
        'actions': [{
            'pub_sub': {
                'topic':
                    'projects/{project_id}/topics/{topic_id}'.format(
                        project_id=PROJECT_ID, topic_id=PUB_SUB_TOPIC)
            }
        }]
    }

    # Create the DLP job and let the DLP api processes it.
    try:
        dlp.create_dlp_job(parent=(parent), inspect_job=(inspect_job))
        log('Job created by create_DLP_job', severity=LOG_SEVERITY_INFO)
    except Exception as e:
        log(e, severity=LOG_SEVERITY_ERROR)


def resolve_DLP(data, context):
    """This function listens to the pub/sub notification from function above.
      As soon as it gets pub/sub notification, it picks up results from the
      DLP job and moves the file to sensitive bucket or nonsensitive bucket
      accordingly.
      Args:
          data: The Cloud Pub/Sub event
      Returns:
          None. Debug information is printed to the log.
      """
    # Get the targeted DLP job name that is created by the create_DLP_job function
    job_name = data['attributes']['DlpJobName']
    log('Received pub/sub notification from DLP job: {}'.format(job_name), severity=LOG_SEVERITY_INFO)

    # Get the DLP job details by the job_name
    job = dlp.get_dlp_job(request={'name': job_name})
    log('Job Name:{name}\nStatus:{status}'.format(name=job.name, status=job.state), severity=LOG_SEVERITY_INFO)

    # Fetching Filename in Cloud Storage from the original dlpJob config.
    # See defintion of "JSON Output' in Limiting Cloud Storage Scans':
    # https://cloud.google.com/dlp/docs/inspecting-storage

    file_path = (
        job.inspect_details.requested_options.job_config.storage_config
            .cloud_storage_options.file_set.url)
    file_name = file_path.split("/", 3)[3]

    info_type_stats = job.inspect_details.result.info_type_stats
    source_bucket = storage_client.get_bucket(STAGING_BUCKET)
    source_blob = source_bucket.blob(file_name)
    if (len(info_type_stats) > 0):
        # Found at least one sensitive data
        for stat in info_type_stats:
            log('Found {stat_cnt} instances of {stat_type_name}.'.format(
                stat_cnt=stat.count, stat_type_name=stat.info_type.name), severity=LOG_SEVERITY_WARNING)
        log('Moving item to sensitive bucket', severity=LOG_SEVERITY_DEBUG)
        destination_bucket = storage_client.get_bucket(SENSITIVE_BUCKET)
        source_bucket.copy_blob(source_blob, destination_bucket,
                                file_name)  # copy the item to the sensitive bucket
        source_blob.delete()  # delete item from the quarantine bucket

    
