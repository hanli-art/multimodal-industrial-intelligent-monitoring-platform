import torch

print("CUDA 是否可用:", torch.cuda.is_available())
print("可用 GPU 数量:", torch.cuda.device_count())
if torch.cuda.is_available():
    print("当前 GPU 名称:", torch.cuda.get_device_name(0))
    print("PyTorch 使用的 CUDA 版本:", torch.version.cuda)