ROCM_ELIF = """    elif 'ROCMExecutionProvider' in available_providers:
        rocm_device_id = int(os.environ.get('ROCR_VISIBLE_DEVICES', '0').split(',')[0])
        rocm_options = {'device_id': rocm_device_id}
        provider_options = [('ROCMExecutionProvider', rocm_options), ('CPUExecutionProvider', {})]
        logger.info(f"ROCm provider available - using AMD GPU (device_id={rocm_device_id})")
    else:
        provider_options = [('CPUExecutionProvider', {})]
        logger.info("No GPU provider available - using CPU only")"""

ROCM_ELIF_INNER = """                            elif 'ROCMExecutionProvider' in available_providers:
                                rocm_device_id = int(os.environ.get('ROCR_VISIBLE_DEVICES', '0').split(',')[0])
                                rocm_options = {'device_id': rocm_device_id}
                                provider_options = [('ROCMExecutionProvider', rocm_options), ('CPUExecutionProvider', {})]
                            else:
                                provider_options = [('CPUExecutionProvider', {})]"""

patches = [
    # clap_analyzer.py — 3 occurrences with logger message
    (
        "/app/tasks/clap_analyzer.py",
        """    else:
        provider_options = [('CPUExecutionProvider', {})]
        logger.info("CUDA provider not available - using CPU only")""",
        ROCM_ELIF,
        3,
    ),
    # analysis.py — 1 occurrence with logger message
    (
        "/app/tasks/analysis.py",
        """    else:
        provider_options = [('CPUExecutionProvider', {})]
        logger.info("CUDA provider not available - using CPU only")""",
        ROCM_ELIF,
        1,
    ),
    # analysis.py — 2 occurrences without logger (deeply indented)
    (
        "/app/tasks/analysis.py",
        """                            else:
                                provider_options = [('CPUExecutionProvider', {})]""",
        ROCM_ELIF_INNER,
        2,
    ),
    # collection_manager.py — treat PocketBase HTTPError as empty results (PostgreSQL deployment)
    (
        "/app/tasks/collection_manager.py",
        """            except requests.exceptions.ConnectTimeout as e:
                logger.error(f"{log_prefix} CRITICAL: Connection to PocketBase timed out while fetching records. This task will be retried. Error: {e}")
                raise  # Re-raise to trigger RQ's retry mechanism""",
        """            except requests.exceptions.HTTPError as e:
                logger.warning(f"{log_prefix} PocketBase not available (HTTP {e.response.status_code if e.response is not None else 'N/A'}), treating as no existing records.")
                remote_embeddings = []
                remote_scores = []
            except requests.exceptions.ConnectTimeout as e:
                logger.error(f"{log_prefix} CRITICAL: Connection to PocketBase timed out while fetching records. This task will be retried. Error: {e}")
                raise  # Re-raise to trigger RQ's retry mechanism""",
        1,
    ),
]

for path, old, new, expected in patches:
    with open(path) as f:
        src = f.read()
    count = src.count(old)
    if count != expected:
        raise RuntimeError(f"{path}: expected {expected} occurrences, found {count}\n---\n{old}\n---")
    src = src.replace(old, new)
    with open(path, "w") as f:
        f.write(src)
    print(f"Patched {count} occurrence(s) in {path}")
