import os

# Create backend directories
os.makedirs("modules", exist_ok=True)
os.makedirs("core", exist_ok=True)

with open("core/__init__.py", "w") as f:
    pass

with open("modules/__init__.py", "w") as f:
    pass
