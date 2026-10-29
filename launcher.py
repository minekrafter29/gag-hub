import subprocess
import sys
import threading
import queue
import tkinter as tk
from tkinter import scrolledtext


class BotLauncher:
    def __init__(self, root):
        self.root = root
        self.root.title("GrowAGrader — Панель управления")
        self.root.geometry("760x520")
        self.root.minsize(620, 420)
        self.root.configure(bg="#111827")

        self.process = None
        self.output_queue = queue.Queue()

        header = tk.Frame(root, bg="#111827")
        header.pack(fill="x", padx=24, pady=(22, 12))

        tk.Label(
            header,
            text="🤖 Панель управления GrowAGrader",
            font=("Segoe UI", 20, "bold"),
            fg="#f9fafb",
            bg="#111827",
        ).pack(anchor="w")

        tk.Label(
            header,
            text="Запуск Telegram-бота и мониторинг логов",
            font=("Segoe UI", 10),
            fg="#9ca3af",
            bg="#111827",
        ).pack(anchor="w", pady=(5, 0))

        status_frame = tk.Frame(root, bg="#1f2937")
        status_frame.pack(fill="x", padx=24, pady=10)

        self.status_dot = tk.Label(
            status_frame,
            text="●",
            font=("Segoe UI", 16),
            fg="#ef4444",
            bg="#1f2937",
        )
        self.status_dot.pack(side="left", padx=(14, 8), pady=12)

        self.status_label = tk.Label(
            status_frame,
            text="Остановлен",
            font=("Segoe UI", 11, "bold"),
            fg="#f9fafb",
            bg="#1f2937",
        )
        self.status_label.pack(side="left")

        buttons = tk.Frame(root, bg="#111827")
        buttons.pack(fill="x", padx=24, pady=10)

        self.start_button = tk.Button(
            buttons,
            text="▶  Запустить бота",
            command=self.start_bot,
            font=("Segoe UI", 10, "bold"),
            fg="white",
            bg="#16a34a",
            activebackground="#15803d",
            activeforeground="white",
            relief="flat",
            padx=16,
            pady=10,
            cursor="hand2",
        )
        self.start_button.pack(side="left")

        self.stop_button = tk.Button(
            buttons,
            text="■  Остановить",
            command=self.stop_bot,
            font=("Segoe UI", 10, "bold"),
            fg="white",
            bg="#dc2626",
            activebackground="#b91c1c",
            activeforeground="white",
            relief="flat",
            padx=16,
            pady=10,
            cursor="hand2",
            state="disabled",
        )
        self.stop_button.pack(side="left", padx=10)

        tk.Label(
            root,
            text="Журнал работы",
            font=("Segoe UI", 11, "bold"),
            fg="#e5e7eb",
            bg="#111827",
        ).pack(anchor="w", padx=24, pady=(10, 6))

        self.log = scrolledtext.ScrolledText(
            root,
            height=14,
            font=("Consolas", 9),
            bg="#0b1220",
            fg="#d1d5db",
            insertbackground="white",
            relief="flat",
            padx=12,
            pady=10,
            wrap="word",
        )
        self.log.pack(fill="both", expand=True, padx=24, pady=(0, 20))
        self.log.insert("end", "Лаунчер готов. Нажмите «Запустить бота».\n")
        self.log.configure(state="disabled")

        self.root.after(100, self.check_output)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_running(self, running):
        if running:
            self.status_dot.configure(fg="#22c55e")
            self.status_label.configure(text="Работает")
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
        else:
            self.status_dot.configure(fg="#ef4444")
            self.status_label.configure(text="Остановлен")
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")

    def start_bot(self):
        if self.process and self.process.poll() is None:
            return

        try:
            self.process = subprocess.Popen(
                [sys.executable, "-u", "main.py"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            self.set_running(True)
            self.write_log("\n--- Запуск бота ---\n")
            threading.Thread(target=self.read_process_output, daemon=True).start()
            threading.Thread(target=self.watch_process, daemon=True).start()
        except FileNotFoundError:
            self.write_log("Ошибка: файл main.py не найден. Положите launcher.py рядом с main.py.\n")
        except Exception as error:
            self.write_log(f"Не удалось запустить бота: {error}\n")

    def read_process_output(self):
        if not self.process or not self.process.stdout:
            return

        for line in self.process.stdout:
            self.output_queue.put(line)

    def watch_process(self):
        process = self.process
        if process:
            code = process.wait()
            self.output_queue.put(f"\n--- Бот завершился с кодом {code} ---\n")
            self.output_queue.put("__PROCESS_FINISHED__")

    def check_output(self):
        try:
            while True:
                text = self.output_queue.get_nowait()
                if text == "__PROCESS_FINISHED__":
                    self.process = None
                    self.set_running(False)
                else:
                    self.write_log(text)
        except queue.Empty:
            pass

        self.root.after(100, self.check_output)

    def stop_bot(self):
        if self.process and self.process.poll() is None:
            self.write_log("\n--- Останавливаю бота ---\n")
            self.process.terminate()
        else:
            self.set_running(False)

    def on_close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = BotLauncher(root)
    root.mainloop()