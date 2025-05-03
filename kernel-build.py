#!/usr/bin/env python3

import argparse
import subprocess
import os
import sys
import shutil
import json
import datetime
import logging
from typing import Union, List, Dict, Any, Optional

class Logger:
    def __init__(self, config: dict):
        self.log_file = config.get("log_file", "/var/log/kernel-build.log")
        log_level_str = config.get("log_level", "INFO").upper()
        level_map = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR
        }
        log_level = level_map.get(log_level_str, logging.INFO)

        # Create log directory if it doesn't exist
        try:
            log_dir = os.path.dirname(self.log_file)
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir, exist_ok=True)
        except OSError as e:
            print(f"Warning: Cannot create log directory: {e}", file=sys.stderr)
            self.log_file = None

        # Configure logging
        self.logger = logging.getLogger("kernel-build")
        self.logger.setLevel(log_level)

        # Remove any existing handlers
        self.logger.handlers = []

        # File handler
        if self.log_file:
            try:
                file_handler = logging.FileHandler(self.log_file)
                file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
                file_handler.setFormatter(file_formatter)
                self.logger.addHandler(file_handler)
            except (OSError, PermissionError) as e:
                print(f"Warning: Cannot write to log file: {e}", file=sys.stderr)

        # Console handler
        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter('%(message)s')
        console_handler.setFormatter(console_formatter)
        self.logger.addHandler(console_handler)

    def debug(self, message):
        self.logger.debug(message)

    def info(self, message):
        self.logger.info(message)

    def warning(self, message):
        self.logger.warning(message)

    def error(self, message):
        self.logger.error(message)

    def command(self, cmd):
        cmd_str = ' '.join(cmd) if isinstance(cmd, list) else cmd
        self.logger.info(f"==> {cmd_str}")

    def command_output(self, output, error=False):
        if not output:
            return
        lines = output.strip().split('\n')
        for line in lines:
            if line:
                if error:
                    self.logger.error(f"   {line}")
                else:
                    self.logger.debug(f"   {line}")

class Config:
    def __init__(self, path):
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Config file not found: {path}")
        try:
            with open(path) as f:
                self.data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in config file: {e}")

        self.validate()
        self.logger = Logger(self.data)

    def validate(self):
        required = [
            "linux_dir", "backup_config_dir", "boot_mountpoint",
            "initramfs_args", "build_jobs", "flash_devices"
        ]
        for key in required:
            if key not in self.data:
                raise ValueError(f"Missing key in config: {key}")

        # Only check if source directory (linux_dir) exists
        linux_dir = self.data["linux_dir"]
        if not os.path.exists(linux_dir):
            raise ValueError(f"Linux source directory does not exist: {linux_dir}")
        if not os.access(linux_dir, os.R_OK | os.W_OK):
            raise ValueError(f"Linux source directory is not readable/writable: {linux_dir}")

    def get(self, key, default=None):
        return self.data.get(key, default)

    def get_flash_devices(self):
        return self.data["flash_devices"]

class Kernel:
    def __init__(self, config: Config):
        self.config = config
        self.linux_dir = config.get("linux_dir")
        self.backup_config_dir = config.get("backup_config_dir")
        self.boot_mountpoint = config.get("boot_mountpoint")
        self.initramfs_args = config.get("initramfs_args")
        self.build_jobs = config.get("build_jobs")
        self._kernel_version = None
        self._build_datetime = None
        self.logger = config.logger
        self._built = False

        # Create required directories
        os.makedirs(self.backup_config_dir, exist_ok=True)
        os.makedirs(self.boot_mountpoint, exist_ok=True)

        # Check for required tools
        self._check_dependencies()

    def _check_dependencies(self):
        """Check if required build tools are available"""
        tools = ["make", "genkernel", "grub-mkconfig"]
        missing = []

        for tool in tools:
            try:
                result = subprocess.run(
                    ["which", tool],
                    text=True,
                    capture_output=True,
                    check=False
                )
                if result.returncode != 0:
                    missing.append(tool)
            except Exception:
                missing.append(tool)

        if missing:
            self.logger.warning(f"Required tools missing: {', '.join(missing)}")
            self.logger.warning("Some operations may fail.")

    @property
    def kernel_version(self):
        if not self._kernel_version:
            rel_file = os.path.join(self.linux_dir, "include/config/kernel.release")
            if os.path.isfile(rel_file):
                try:
                    with open(rel_file) as f:
                        self._kernel_version = f.read().strip()
                except IOError as e:
                    self.logger.error(f"Cannot read kernel version file: {e}")
                    self._kernel_version = "unknown"
            else:
                vmlinux_file = os.path.join(self.linux_dir, "vmlinux")
                if os.path.isfile(vmlinux_file):
                    try:
                        result = subprocess.run(
                            ["strings", vmlinux_file, "|", "grep", "Linux version"],
                            shell=True,
                            text=True,
                            capture_output=True
                        )
                        if result.returncode == 0 and result.stdout:
                            # Extract version from "Linux version X.Y.Z ..."
                            version_line = result.stdout.strip().split("\n")[0]
                            parts = version_line.split()
                            if len(parts) >= 3:
                                self._kernel_version = parts[2]
                            else:
                                self._kernel_version = "unknown"
                        else:
                            self._kernel_version = "unknown"
                    except Exception:
                        self._kernel_version = "unknown"
                else:
                    self._kernel_version = "unknown"
        return self._kernel_version

    @property
    def build_datetime(self):
        if not self._build_datetime:
            self._build_datetime = datetime.datetime.now().strftime("%Y_%m_%d-%H_%M_%S")
        return self._build_datetime

    def run(self, cmd: Union[List[str], str], cwd: Optional[str] = None, check: bool = True) -> subprocess.CompletedProcess:
        self.logger.command(cmd)
        try:
            result = subprocess.run(
                cmd,
                shell=isinstance(cmd, str),
                cwd=cwd,
                text=True,
                capture_output=True
            )

            # Log stdout and stderr
            if result.stdout:
                self.logger.command_output(result.stdout)
            if result.stderr:
                self.logger.command_output(result.stderr, error=True)

            if check and result.returncode != 0:
                self.logger.error(f"Command failed with exit code {result.returncode}")
                sys.exit(result.returncode)
            return result
        except Exception as e:
            self.logger.error(f"Failed to execute command: {e}")
            if check:
                sys.exit(1)
            raise

    def backup_config(self):
        src = os.path.join(self.linux_dir, ".config")
        if os.path.exists(src):
            try:
                os.makedirs(self.backup_config_dir, exist_ok=True)
                dt = datetime.datetime.now().strftime("%Y.%m.%d-%H:%M:%S")
                dst = os.path.join(self.backup_config_dir, f".config.{dt}")
                shutil.copy2(src, dst)
                self.logger.info(f"Старая конфигурация ядра сохранена: {dst}")
            except (OSError, shutil.Error) as e:
                self.logger.error(f"Ошибка при бэкапе конфигурации: {e}")
        else:
            self.logger.info(f".config не найден, пропуск бэкапа.")

    def configure(self):
        self.backup_config()

        # For interactive ncurses UI, we can't capture output
        self.logger.command(["make", f"-j{self.build_jobs}", "nconfig"])
        try:
            # Run without capturing output to allow interactive UI
            result = subprocess.run(
                ["make", f"-j{self.build_jobs}", "nconfig"],
                cwd=self.linux_dir,
                check=True
            )
            if result.returncode != 0:
                self.logger.error(f"Конфигурация завершилась с ошибкой {result.returncode}")
                sys.exit(result.returncode)
        except Exception as e:
            self.logger.error(f"Ошибка при запуске nconfig: {e}")
            sys.exit(1)

        config_path = os.path.join(self.linux_dir, ".config")
        backup_path = os.path.join(self.backup_config_dir, ".config.latest")
        if os.path.exists(config_path):
            try:
                shutil.copy2(config_path, backup_path)
                self.logger.info(f"Текущий .config скопирован в {backup_path}")
            except (OSError, shutil.Error) as e:
                self.logger.error(f"Ошибка при копировании .config: {e}")

    def build(self):
        """Build the kernel only, not the initramfs"""
        self.logger.info("---> Сборка ядра <---")
        self.run(["make", f"-j{self.build_jobs}", "bzImage", "modules"], cwd=self.linux_dir)
        self.logger.info("Сборка ядра завершена.")
        self._built = True

    def is_kernel_built(self) -> bool:
        """Check if kernel has been built"""
        if self._built:
            return True

        # Check for kernel binary
        vmlinuz = os.path.join(self.linux_dir, "arch/x86/boot/bzImage")
        if not os.path.exists(vmlinuz):
            self.logger.warning("Ядро не скомпилировано (bzImage не найден)")
            return False

        return True

    def install(self):
        """Install the built kernel and modules, build initramfs"""
        if not self.is_kernel_built():
            self.logger.error("Ядро не скомпилировано. Сначала выполните 'build'")
            sys.exit(1)

        self.logger.info("---> Установка ядра <---")
        self.run(["make", "modules_install", "install"], cwd=self.linux_dir)

        self.logger.info("---> Сборка и установка initramfs <---")
        genkernel_cmd = ["genkernel"] + self.initramfs_args + ["initramfs"]
        self.run(genkernel_cmd)

        self.logger.info("---> Переименование файлов ядра <---")
        self.rename_kernel_files()
        self.run(["grub-mkconfig", "-o", "/boot/grub/grub.cfg"])
        self.logger.info("Установка завершена.")

    def rename_kernel_files(self):
        dt = self.build_datetime
        kv = self.kernel_version
        boot = self.boot_mountpoint

        # Ensure boot directory exists
        if not os.path.exists(boot):
            self.logger.error(f"Boot directory {boot} does not exist")
            return

        for f in ["System.map", "vmlinuz"]:
            src = os.path.join(boot, f)
            if os.path.exists(src):
                dst = os.path.join(boot, f"{f}-{kv}-{dt}")
                self.logger.info(f"{src} -> {dst}")
                try:
                    shutil.copy2(src, dst)
                except (OSError, shutil.Error) as e:
                    self.logger.error(f"Ошибка при копировании {src}: {e}")

        irfs_name = f"initramfs-{kv}.img"
        irfs_src = os.path.join(boot, irfs_name)
        if os.path.exists(irfs_src):
            irfs_dst = os.path.join(boot, f"initramfs-{kv}-{dt}.img")
            self.logger.info(f"{irfs_src} -> {irfs_dst}")
            try:
                shutil.copy2(irfs_src, irfs_dst)
            except (OSError, shutil.Error) as e:
                self.logger.error(f"Ошибка при копировании {irfs_src}: {e}")

class BootDevice:
    def __init__(self, device: str, partition: str, mountpoint: str, logger: Logger):
        self.device = device
        self.partition = partition
        self.mountpoint = mountpoint
        self.logger = logger

        # Verify devices exist
        self.verify_device()

    def verify_device(self):
        """Check if the device exists"""
        dev = self.device + self.partition
        if not os.path.exists(dev):
            self.logger.warning(f"Устройство {dev} не найдено!")

    def mount(self):
        dev = self.device + self.partition
        self.logger.info(f"Монтирование {dev} в {self.mountpoint}")
        try:
            result = subprocess.run(["mount", dev, self.mountpoint],
                                   text=True, capture_output=True, check=True)
            if result.stdout:
                self.logger.command_output(result.stdout)
        except subprocess.CalledProcessError as e:
            self.logger.error(f"Ошибка при монтировании {dev}: {e}")
            self.logger.error(e.stderr)
            raise

    def umount(self):
        self.logger.info(f"Отмонтирование {self.mountpoint}")
        try:
            result = subprocess.run(["umount", self.mountpoint],
                                   text=True, capture_output=True, check=False)
            if result.stdout:
                self.logger.command_output(result.stdout)
            if result.stderr and "not mounted" not in result.stderr.lower():
                self.logger.command_output(result.stderr, error=True)
        except Exception as e:
            self.logger.error(f"Ошибка при отмонтировании {self.mountpoint}: {e}")

    def show_boot(self):
        try:
            result = subprocess.run(["ls", "-la", self.mountpoint],
                                  text=True, capture_output=True)
            self.logger.info(f"Содержимое {self.mountpoint}:")
            self.logger.command_output(result.stdout)
            if result.stderr:
                self.logger.command_output(result.stderr, error=True)
        except Exception as e:
            self.logger.error(f"Ошибка при просмотре содержимого {self.mountpoint}: {e}")

    def find_grub_path(self) -> str:
        """Find the correct grub.cfg path on the mounted device"""
        possible_paths = [
            os.path.join(self.mountpoint, "grub"),
            os.path.join(self.mountpoint, "boot/grub"),
            os.path.join(self.mountpoint, "grub2"),
            os.path.join(self.mountpoint, "boot/grub2")
        ]

        for path in possible_paths:
            if os.path.isdir(path):
                return os.path.join(path, "grub.cfg")

        # Default to standard path if not found
        self.logger.warning("Grub directory not found, using default path")
        return os.path.join(self.mountpoint, "grub/grub.cfg")

    def install_kernel(self, kernel: Kernel):
        try:
            self.umount()
            self.mount()
            self.logger.info(f"---> Файлы в {self.mountpoint} до установки:")
            self.show_boot()

            # Only install - no building
            kernel.install()

            self.logger.info(f"---> Файлы в {self.mountpoint} после установки:")
            self.show_boot()
            self.logger.info("---> Генерация grub.cfg на устройстве")

            grub_cfg_path = self.find_grub_path()
            grub_dir = os.path.dirname(grub_cfg_path)

            # Create directory if needed
            if not os.path.exists(grub_dir):
                os.makedirs(grub_dir, exist_ok=True)

            result = subprocess.run(["grub-mkconfig", "-o", grub_cfg_path],
                                  text=True, capture_output=True)

            if result.stdout:
                self.logger.command_output(result.stdout)
            if result.stderr:
                self.logger.command_output(result.stderr, error=True)

            if result.returncode != 0:
                self.logger.error(f"Ошибка при создании grub.cfg: код {result.returncode}")
        except Exception as e:
            self.logger.error(f"Ошибка при установке ядра: {e}")
        finally:
            self.umount()

def main():
    parser = argparse.ArgumentParser(
        description="Gentoo Kernel builder",
        formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("command", choices=["build", "configure", "install"])
    parser.add_argument("-c", "--config", default="/etc/kernel_builder.json", help="Путь к json-конфигу")
    args = parser.parse_args()

    try:
        config = Config(args.config)
        kernel = Kernel(config)
    except Exception as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        if args.command == "configure":
            kernel.configure()
        elif args.command == "build":
            kernel.build()  # Only build, no installation
        elif args.command == "install":
            # First check if kernel is built
            if not kernel.is_kernel_built():
                config.logger.error("Ядро не скомпилировано. Сначала выполните 'build'")
                sys.exit(1)

            flashlist = config.get_flash_devices()
            for flash in flashlist:
                device = BootDevice(
                    device=flash["device"],
                    partition=flash["partition"],
                    mountpoint=config.get("boot_mountpoint"),
                    logger=config.logger
                )
                device.install_kernel(kernel)  # This will install only, not build
        else:
            config.logger.error("Неизвестная команда")
            sys.exit(2)
    except KeyboardInterrupt:
        config.logger.error("Прервано пользователем")
        sys.exit(130)
    except Exception as e:
        config.logger.error(f"Критическая ошибка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
