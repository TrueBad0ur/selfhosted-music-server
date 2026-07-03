path = "/app/tasks/clap_analyzer.py"
with open(path) as f:
    src = f.read()

old = """    else:
        provider_options = [('CPUExecutionProvider', {})]
        logger.info("CUDA provider not available - using CPU only")"""

new = """    elif 'ROCMExecutionProvider' in available_providers:
        rocm_device_id = int(os.environ.get('ROCR_VISIBLE_DEVICES', '0').split(',')[0])
        rocm_options = {'device_id': rocm_device_id}
        provider_options = [('ROCMExecutionProvider', rocm_options), ('CPUExecutionProvider', {})]
        logger.info(f"ROCm provider available - using AMD GPU (device_id={rocm_device_id})")
    else:
        provider_options = [('CPUExecutionProvider', {})]
        logger.info("No GPU provider available - using CPU only")"""

count = src.count(old)
if count == 0:
    raise RuntimeError("Pattern not found in clap_analyzer.py")
src = src.replace(old, new)
with open(path, "w") as f:
    f.write(src)
print(f"Patched {count} occurrence(s)")
