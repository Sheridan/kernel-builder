#!/usr/bin/env python3

import argparse
import subprocess
import os
import sys
import shutil
import json
import datetime

class Config:
    def __init__(self, path):
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path) as f:
            self.data = json.load(f)
        self.validate()

    def validate(self):
        required = [
            "linux_dir", "backup_config_dir", "boot_mountpoint",
            "initramfs_args", "build_jobs", "flash_devices"
        ]
        for key in required:
            if key not in self.data:
                raise ValueError(f"Missing key in config: {key}")

    def get(self, key):
        return self.data[key]

    def get_flash_devices(self):
        return self.data["flash_devices"]

class Kernel:
    def __init__(self, config: Config):
        self.linux_dir = config.get("linux_dir")
        self.backup_config_dir = config.get("backup_config_dir")
        self.boot_mountpoint = config.get("boot_mountpoint")
        self.initramfs_args = config.get("initramfs_args")
        self.build_jobs = config.get("build_jobs")
        self._kernel_version = None
        self._build_datetime = None

    @property
    def kernel_version(self):
        if not self._kernel_version:
            rel_file = os.path.join(self.linux_dir, "include/config/kernel.release")
            if os.path.isfile(rel_file):
                with open(rel_file) as f:
                    self._kernel_version = f.read().strip()
            else:
                self._kernel_version = "unknown"
        return self._kernel_version

    @property
    def build_datetime(self):
        if not self._build_datetime:
            self._build_datetime = datetime.datetime.now().strftime("%Y_%m_%d-%H_%M_%S")
        return self._build_datetime

    def run(self, cmd, cwd=None, check=True):
        print(f"==> {' '.join(cmd) if isinstance(cmd, list) else cmd}")
        result = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=cwd)
        if check and result.returncode != 0:
            print(f"Ошибка выполнения: {cmd}", file=sys.stderr)
            sys.exit(result.returncode)

    def backup_config(self):
        src = os.path.join(self.linux_dir, ".config")
        if os.path.exists(src):
            os.makedirs(self.backup_config_dir, exist_ok=True)
            dt = datetime.datetime.now().strftime("%Y.%m.%d-%H:%M:%S")
            dst = os.path.join(self.backup_config_dir, f".config.{dt}")
            shutil.copy2(src, dst)
            print(f"Старая конфигурация ядра сохранена: {dst}")
        else:
            print(f".config не найден, пропуск бэкапа.")

    def configure(self):
        self.backup_config()
        self.run(["make", f"-j{self.build_jobs}", "nconfig"], cwd=self.linux_dir)
        config_path = os.path.join(self.linux_dir, ".config")
        backup_path = os.path.join(self.backup_config_dir, ".config.latest")
        if os.path.exists(config_path):
            shutil.copy2(config_path, backup_path)
            print(f"Текущий .config скопирован в {backup_path}")

    def build(self):
        print("---> Сборка ядра <---")
        self.run(["make", f"-j{self.build_jobs}", "bzImage", "modules"], cwd=self.linux_dir)
        self.run(["make", "modules_install", "install"], cwd=self.linux_dir)
        print("---> Сборка initramfs <---")
        genkernel_cmd = ["genkernel"] + self.initramfs_args + ["initramfs"]
        self.run(genkernel_cmd)
        print("---> Переименование файлов ядра <---")
        self.rename_kernel_files()
        self.run(["grub-mkconfig", "-o", "/boot/grub/grub.cfg"])
        print("Сборка завершена.")

    def rename_kernel_files(self):
        dt = self.build_datetime
        kv = self.kernel_version
        boot = self.boot_mountpoint
        for f in ["System.map", "vmlinuz"]:
            src = os.path.join(boot, f)
            if os.path.exists(src):
                dst = os.path.join(boot, f"{f}-{kv}-{dt}")
                print(f"{src} -> {dst}")
                shutil.copy2(src, dst)
        irfs_name = f"initramfs-{kv}.img"
        irfs_src = os.path.join(boot, irfs_name)
        if os.path.exists(irfs_src):
            irfs_dst = os.path.join(boot, f"initramfs-{kv}-{dt}.img")
            print(f"{irfs_src} -> {irfs_dst}")
            shutil.copy2(irfs_src, irfs_dst)

class BootDevice:
    def __init__(self, device, partition, mountpoint):
        self.device = device
        self.partition = partition
        self.mountpoint = mountpoint

    def mount(self):
        dev = self.device + self.partition
        print(f"Монтирование {dev} в {self.mountpoint}")
        subprocess.run(["mount", dev, self.mountpoint], check=True)

    def umount(self):
        print(f"Отмонтирование {self.mountpoint}")
        subprocess.run(["umount", self.mountpoint], check=False)

    def show_boot(self):
        subprocess.run(["ls", "-la", self.mountpoint])

    def install_kernel(self, kernel: Kernel):
        self.umount()
        self.mount()
        print(f"---> Файлы в {self.mountpoint} до установки:")
        self.show_boot()
        kernel.build()
        print(f"---> Файлы в {self.mountpoint} после установки:")
        self.show_boot()
        print("---> Генерация grub.cfg на устройстве")
        subprocess.run(["grub-mkconfig", "-o", os.path.join(self.mountpoint, "grub/grub.cfg")], check=True)
        self.umount()

def main():
    parser = argparse.ArgumentParser(
        description="Gentoo Kernel builder",
        formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("command", choices=["build", "configure", "install"])
    parser.add_argument("-c", "--config", default="kernel_builder.json", help="Путь к json-конфигу")
    args = parser.parse_args()

    try:
        config = Config(args.config)
        kernel = Kernel(config)
    except Exception as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)

    if args.command == "configure":
        kernel.configure()
    elif args.command == "build":
        kernel.build()
    elif args.command == "install":
        flashlist = config.get_flash_devices()
        for flash in flashlist:
            device = BootDevice(
                device=flash["device"],
                partition=flash["partition"],
                mountpoint=config.get("boot_mountpoint")
            )
            device.install_kernel(kernel)
    else:
        print("Неизвестная команда")
        sys.exit(2)

if __name__ == "__main__":
    main()
