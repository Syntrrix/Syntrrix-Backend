import os
import json
import shutil
import logging
from pathlib import Path
from django.conf import settings

logger = logging.getLogger(__name__)

# Initialize AWS S3 configuration
_s3_client = None
_s3_bucket = None

if hasattr(settings, 'AWS_STORAGE_BUCKET_NAME') and settings.AWS_STORAGE_BUCKET_NAME:
    try:
        import boto3
        s3_kwargs = {}
        if getattr(settings, 'AWS_ACCESS_KEY_ID', None):
            s3_kwargs['aws_access_key_id'] = settings.AWS_ACCESS_KEY_ID
        if getattr(settings, 'AWS_SECRET_ACCESS_KEY', None):
            s3_kwargs['aws_secret_access_key'] = settings.AWS_SECRET_ACCESS_KEY
        if getattr(settings, 'AWS_S3_REGION_NAME', None):
            s3_kwargs['region_name'] = settings.AWS_S3_REGION_NAME
            
        _s3_client = boto3.client('s3', **s3_kwargs)
        _s3_bucket = settings.AWS_STORAGE_BUCKET_NAME
        logger.info(f"Initialized persistent S3 storage with bucket: {_s3_bucket}")
    except ImportError:
        logger.error("AWS_STORAGE_BUCKET_NAME is configured, but 'boto3' is not installed. Falling back to local storage.")
    except Exception as e:
        logger.error(f"Failed to initialize S3 client: {e}. Falling back to local storage.")

# Local persistent data directory variable
_local_data_root = None


def migrate_existing_files(old_media_root: str, new_persistent_root: str):
    """
    One-time migration helper to copy existing Data Hub/Live Chat JSONs
    from the old media path to the new persistent path.
    """
    if not old_media_root or not new_persistent_root:
        return
    try:
        old_media_root = os.path.abspath(old_media_root)
        new_persistent_root = os.path.abspath(new_persistent_root)
        if old_media_root == new_persistent_root:
            return

        for folder in ['data_hub', 'live_chat']:
            old_path = os.path.join(old_media_root, folder)
            new_path = os.path.join(new_persistent_root, folder)
            if os.path.exists(old_path) and os.path.isdir(old_path):
                logger.info(f"Checking for existing data to migrate in {old_path}...")
                for root, dirs, files in os.walk(old_path):
                    for file in files:
                        src_file = os.path.join(root, file)
                        # Compute path relative to old_media_root
                        rel_file = os.path.relpath(src_file, old_media_root)
                        dst_file = os.path.join(new_persistent_root, rel_file)
                        
                        os.makedirs(os.path.dirname(dst_file), exist_ok=True)
                        if not os.path.exists(dst_file):
                            shutil.copy2(src_file, dst_file)
                            logger.info(f"Migrated {rel_file} to persistent directory: {dst_file}")
    except Exception as e:
        logger.error(f"Error migrating existing files: {e}", exc_info=True)


def migrate_data_from_old_paths(new_root: str):
    """
    Copies files from '/var/lib/agrobridge_data' or settings.BASE_DIR/persistent_data
    into the new_root if they exist, to prevent any data loss when switching paths.
    """
    if not new_root:
        return
    import shutil
    new_root_abs = os.path.abspath(new_root)
    
    old_paths = [
        '/var/lib/agrobridge_data',
        os.path.join(str(settings.BASE_DIR), 'persistent_data')
    ]
    
    for old_path in old_paths:
        if not os.path.exists(old_path):
            continue
        old_path_abs = os.path.abspath(old_path)
        if old_path_abs == new_root_abs:
            continue
            
        logger.info(f"DataHub Migration: Found old data in {old_path_abs}. Migrating to {new_root_abs}...")
        try:
            for folder in ['data_hub', 'live_chat']:
                src_dir = os.path.join(old_path_abs, folder)
                dst_dir = os.path.join(new_root_abs, folder)
                if os.path.exists(src_dir) and os.path.isdir(src_dir):
                    for root, dirs, files in os.walk(src_dir):
                        for file in files:
                            src_file = os.path.join(root, file)
                            rel_file = os.path.relpath(src_file, src_dir)
                            dst_file = os.path.join(dst_dir, rel_file)
                            
                            os.makedirs(os.path.dirname(dst_file), exist_ok=True)
                            if not os.path.exists(dst_file):
                                shutil.copy2(src_file, dst_file)
                                logger.info(f"DataHub Migration: Copied {folder}/{rel_file} to {dst_file}")
        except Exception as e:
            logger.error(f"Error migrating files from {old_path}: {e}")


def get_persistent_data_root() -> str:
    """
    Resolves the writable local directory to be used for storing JSON data files.
    """
    global _local_data_root
    if _local_data_root is not None:
        return _local_data_root

    # Try configured PERSISTENT_DATA_ROOT
    configured_root = getattr(settings, 'PERSISTENT_DATA_ROOT', None)
    if configured_root:
        root_path = str(configured_root)
        try:
            os.makedirs(root_path, exist_ok=True)
            _local_data_root = root_path
            # Trigger migrations
            migrate_data_from_old_paths(_local_data_root)
            old_media = getattr(settings, 'MEDIA_ROOT', None)
            if old_media:
                migrate_existing_files(str(old_media), _local_data_root)
            return _local_data_root
        except Exception as e:
            logger.warning(f"Configured PERSISTENT_DATA_ROOT ({root_path}) is not writable: {e}. Trying candidates...")

    # Autodetect writable persistent paths on Linux/Production
    if os.name != 'nt':
        candidate_paths = [
            '/home/ubuntu/agrobridge_data',
            '/var/tmp/agrobridge_data',
            os.path.expanduser('~/agrobridge_data'),
            '/var/lib/agrobridge_data',
            # Project root fallback
            os.path.join(str(settings.BASE_DIR), 'persistent_data')
        ]
        for path in candidate_paths:
            try:
                os.makedirs(path, exist_ok=True)
                # Test write capability
                test_file = os.path.join(path, '.write_test')
                with open(test_file, 'w') as f:
                    f.write('test')
                os.remove(test_file)
                
                _local_data_root = path
                logger.info(f"Auto-detected writable persistent data root: {_local_data_root}")
                
                # Trigger migrations
                migrate_data_from_old_paths(_local_data_root)
                old_media = getattr(settings, 'MEDIA_ROOT', None)
                if old_media:
                    migrate_existing_files(str(old_media), _local_data_root)
                return _local_data_root
            except Exception:
                continue

    # Windows or final fallback
    fallback_path = os.path.join(str(settings.BASE_DIR), 'persistent_data')
    os.makedirs(fallback_path, exist_ok=True)
    _local_data_root = fallback_path
    logger.info(f"Using default persistent data root: {_local_data_root}")
    
    migrate_data_from_old_paths(_local_data_root)
    old_media = getattr(settings, 'MEDIA_ROOT', None)
    if old_media:
        migrate_existing_files(str(old_media), _local_data_root)
        
    return _local_data_root


def get_data_hub_dir(seller_user_id: int) -> str:
    """
    Returns the path to the seller's Data Hub directory.
    """
    root = get_persistent_data_root()
    return os.path.join(root, "data_hub", str(int(seller_user_id)))


def get_live_chat_dir() -> str:
    """
    Returns the path to the Live Chat directory.
    """
    root = get_persistent_data_root()
    return os.path.join(root, "live_chat")
