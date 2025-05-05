#!/usr/bin/env python3

import argparse
import subprocess
import os
import sys
import shutil
import json
import datetime
import logging

class Logger:
    def __init__(self, config: dict):
        self.log_file = config.get("log_file", "/var/log/kernel-build.log")

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
        self.logger.setLevel(logging.INFO)
        self.logger.handlers = []

        # File handler
        if self.log_file:
            try:
                file_handler = logging.FileHandler(self.log_file)
                file_formatter = logging.Formatter('%(asctime)s - %(message)s')
                file_handler.setFormatter(file_formatter)
                self.logger.addHandler(file_handler)
            except Exception as e:
                print(f"Warning: Cannot write to log file: {e}", file=sys.stderr)

        # Console handler
        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter('%(message)s')
        console_handler.setFormatter(console_formatter)
        self.logger.addHandler(console_handler)

    def info(self, message):
        self.logger.info(message)

    def error(self, message):
        self.logger.error(message)

    def command(self, cmd):
        cmd_str = ' '.join(cmd) if isinstance(cmd, list) else cmd
        self.logger.info(f"==> {cmd_str}")

    def command_output(self, line, error=False):
        """Process a single line of output"""
        if not line:
            return

        # Always log to file
        if error:
            self.logger.error(f"   {line}")
        else:
            self.logger.info(f"   {line}")

        # For console: overwrite the previous line
        print(f"\r   {line}", end="", flush=True)


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
        self.temp_dir = None

        # Create required directories
        os.makedirs(self.backup_config_dir, exist_ok=True)
        os.makedirs(self.boot_mountpoint, exist_ok=True)

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
        self.logger.command(cmd)

        # Use Popen to capture output in real-time
        process = subprocess.Popen(
            cmd,
            shell=isinstance(cmd, str),
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            universal_newlines=True
        )

        # Process stdout and stderr streams
        while True:
            stdout_line = process.stdout.readline()
            if stdout_line:
                self.logger.command_output(stdout_line.rstrip())

            stderr_line = process.stderr.readline()
            if stderr_line:
                self.logger.command_output(stderr_line.rstrip(), error=True)

            # Check if process has finished
            if process.poll() is not None:
                break

        # Get return code
        return_code = process.poll()

        # Print a newline to prepare for next output
        print()

        if check and return_code != 0:
            self.logger.error(f"Command failed with exit code {return_code}")
            sys.exit(return_code)

        return return_code

    def backup_config(self):
        src = os.path.join(self.linux_dir, ".config")
        if os.path.exists(src):
            os.makedirs(self.backup_config_dir, exist_ok=True)
            dt = datetime.datetime.now().strftime("%Y.%m.%d-%H:%M:%S")
            dst = os.path.join(self.backup_config_dir, f".config.{dt}")
            shutil.copy2(src, dst)
            self.logger.info(f"Старая конфигурация ядра сохранена: {dst}")
        else:
            self.logger.info(f".config не найден, пропуск бэкапа.")

    def configure(self):
        self.backup_config()

        # For interactive ncurses UI, we can't capture output
        self.logger.command(["make", f"-j{self.build_jobs}", "nconfig"])

        # Run without capturing output to allow interactive UI
        result = subprocess.run(
            ["make", f"-j{self.build_jobs}", "nconfig"],
            cwd=self.linux_dir,
            check=False
        )
        if result.returncode != 0:
            self.logger.error(f"Конфигурация завершилась с ошибкой {result.returncode}")
            sys.exit(result.returncode)

        config_path = os.path.join(self.linux_dir, ".config")
        backup_path = os.path.join(self.backup_config_dir, ".config.latest")
        if os.path.exists(config_path):
            shutil.copy2(config_path, backup_path)
            self.logger.info(f"Текущий .config скопирован в {backup_path}")

    def build(self):
        """Build the kernel only, not the initramfs"""
        self.logger.info("---> Сборка ядра <---")
        self.run(["make", f"-j{self.build_jobs}", "bzImage", "modules"], cwd=self.linux_dir)
        self.logger.info("Сборка ядра завершена.")
        self._built = True

    def is_kernel_built(self):
        """Check if kernel has been built"""
        if self._built:
            return True

        # Check for kernel binary
        vmlinuz = os.path.join(self.linux_dir, "arch/x86/boot/bzImage")
        if not os.path.exists(vmlinuz):
            self.logger.info("Ядро не скомпилировано (bzImage не найден)")
            return False

        return True

    def create_temp_dir(self):
        """Create a temporary directory for kernel installation"""
        self.temp_dir = os.path.join("/tmp", f"kernel-build-{self.build_datetime}")
        os.makedirs(self.temp_dir, exist_ok=True)
        temp_boot = os.path.join(self.temp_dir, "boot")
        os.makedirs(temp_boot, exist_ok=True)
        self.logger.info(f"Created temporary directory: {self.temp_dir}")
        return self.temp_dir

    def install(self):
        """Install the built kernel and modules, build initramfs"""
        if not self.is_kernel_built():
            self.logger.error("Ядро не скомпилировано. Сначала выполните 'build'")
            sys.exit(1)

        # Create temporary directory
        temp_dir = self.create_temp_dir()
        temp_boot = os.path.join(temp_dir, "boot")

        self.logger.info("---> Установка ядра во временную директорию <---")
        # Install to temp directory
        self.run(["make", "install", f"INSTALL_PATH={temp_boot}"], cwd=self.linux_dir)

        self.logger.info("---> Сборка и установка initramfs во временную директорию <---")
        # Update this line to use correct genkernel syntax
        # genkernel_cmd = ["genkernel"] + self.initramfs_args + [f"--kernel-filenames={temp_boot}", "initramfs"]
        genkernel_cmd = ["genkernel"] + self.initramfs_args + [f"--bootdir={temp_boot}", "initramfs"]
        self.run(genkernel_cmd)

        self.logger.info("Ядро и initramfs подготовлены во временной директории.")
        return temp_dir


    def install_to_device(self, device):
        """Install modules and copy kernel files to the real location"""
        self.logger.info("---> Установка модулей ядра <---")
        self.run(["make", "modules_install"], cwd=self.linux_dir)

        self.logger.info("---> Копирование файлов ядра с временным суффиксом <---")
        dt = self.build_datetime
        kv = self.kernel_version

        # Copy files from temp dir to real boot mountpoint
        temp_boot = os.path.join(self.temp_dir, "boot")
        for f in os.listdir(temp_boot):
            src = os.path.join(temp_boot, f)
            if os.path.isfile(src):
                # Add datetime suffix to filename
                base, ext = os.path.splitext(f)
                if not ext:  # For files like "vmlinuz" with no extension
                    dst = os.path.join(device.mountpoint, f"{f}-{kv}-{dt}")
                else:
                    dst = os.path.join(device.mountpoint, f"{base}-{kv}-{dt}{ext}")

                self.logger.info(f"{src} -> {dst}")
                shutil.copy2(src, dst)

        self.run(["grub-mkconfig", "-o", "/boot/grub/grub.cfg"])
        self.logger.info("Установка завершена.")

class BootDevice:
    def __init__(self, device, partition, mountpoint, logger):
        self.device = device
        self.partition = partition
        self.mountpoint = mountpoint
        self.logger = logger

    def mount(self):
        dev = self.device + self.partition
        self.logger.info(f"Монтирование {dev} в {self.mountpoint}")
        result = subprocess.run(["mount", dev, self.mountpoint],
                               text=True, capture_output=True, check=True)
        if result.stdout:
            self.logger.command_output(result.stdout)

    def umount(self):
        self.logger.info(f"Отмонтирование {self.mountpoint}")
        subprocess.run(["umount", self.mountpoint], check=False)

    def show_boot(self):
        self.logger.info(f"Содержимое {self.mountpoint}:")
        try:
            for filename in os.listdir(self.mountpoint):
                file_path = os.path.join(self.mountpoint, filename)
                if os.path.isfile(file_path):
                    stat_info = os.stat(file_path)
                    # Get modification time in a readable format
                    mod_time = datetime.datetime.fromtimestamp(stat_info.st_mtime)
                    time_str = mod_time.strftime("%Y-%m-%d %H:%M:%S")
                    self.logger.command_output(f"{filename:<30} {time_str}")
        except Exception as e:
            self.logger.error(f"Ошибка при чтении содержимого {self.mountpoint}: {e}")

    def find_grub_path(self):
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
        return os.path.join(self.mountpoint, "grub/grub.cfg")

    def install_grub_bootloader(self):
        """Install GRUB bootloader to the device's boot sector for both BIOS and EFI if available"""
        self.logger.info(f"---> Установка GRUB в загрузочную область устройства {self.device} <---")

        success = False

        # Install for BIOS boot
        self.logger.info(f"Установка GRUB для BIOS (i386-pc)")
        bios_cmd = ["grub-install", "--target=i386-pc", "--recheck",
                    f"--boot-directory={self.mountpoint}", self.device]
        bios_result = subprocess.run(bios_cmd, text=True, capture_output=True, check=False)

        if bios_result.stdout:
            self.logger.command_output(bios_result.stdout)
        if bios_result.stderr:
            self.logger.command_output(bios_result.stderr, error=True)

        if bios_result.returncode == 0:
            success = True
            self.logger.info(f"GRUB успешно установлен для BIOS на устройство {self.device}")
        else:
            self.logger.error(f"Установка GRUB для BIOS завершилась с ошибкой")

        # EFI installation - the mountpoint itself is the EFI directory for flash drives
        # Add --removable flag for compatibility with more systems
        self.logger.info(f"Установка GRUB для UEFI (x86_64-efi)")
        efi_cmd = ["grub-install", "--target=x86_64-efi", "--recheck",
                  f"--efi-directory={self.mountpoint}", "--bootloader-id=GRUB",
                  "--removable", self.device]
        efi_result = subprocess.run(efi_cmd, text=True, capture_output=True, check=False)

        if efi_result.stdout:
            self.logger.command_output(efi_result.stdout)
        if efi_result.stderr:
            self.logger.command_output(efi_result.stderr, error=True)

        if efi_result.returncode == 0:
            success = True
            self.logger.info(f"GRUB успешно установлен для UEFI на устройство {self.device}")
        else:
            self.logger.error(f"Установка GRUB для UEFI завершилась с ошибкой")

        if not success:
            self.logger.error(f"Установка GRUB не удалась ни в одном режиме")

    def install_kernel(self, kernel):
        try:
            self.umount()
            self.mount()
            self.logger.info(f"---> Файлы в {self.mountpoint} до установки:")
            self.show_boot()

            # Prepare kernel files in temporary directory
            if not kernel.temp_dir:
                kernel.install()

            # Install modules and copy files to actual device
            kernel.install_to_device(self)

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

            # Install GRUB bootloader to the device
            self.install_grub_bootloader()
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
            kernel.build()
        elif args.command == "install":
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
                device.install_kernel(kernel)
    except KeyboardInterrupt:
        config.logger.error("Прервано пользователем")
        sys.exit(130)
    except Exception as e:
        config.logger.error(f"Критическая ошибка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
