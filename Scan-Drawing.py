import re
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


DIMENSION_PATTERN = re.compile(
    r'(?<![\w.])(?:Ø|⌀|R)?\s*-?\d+(?:[.,]\d+)?\s*(?:mm|cm|m|in(?:ch(?:es)?)?|["\'])?(?!\w)',
    re.IGNORECASE,
)
RENDER_SCALE = 2.0


def main() -> None:
    root = tk.Tk()
    root.withdraw()

    try:
        import fitz
        from PIL import Image, ImageTk
    except ImportError as exc:
        messagebox.showerror(
            "Missing Python package",
            "A required package for opening PDFs is missing.\n\n"
            f"Install it for this interpreter with:\n{sys.executable} -m pip install pymupdf pillow\n\n"
            f"Details: {exc}",
            parent=root,
        )
        root.destroy()
        raise SystemExit(1) from exc

    selected = filedialog.askopenfilename(
        title="Select a PDF drawing",
        filetypes=[("PDF files", "*.pdf")],
        parent=root,
    )
    if not selected:
        root.destroy()
        raise SystemExit("PDF selection cancelled.")

    selected_pdf = Path(selected)
    if selected_pdf.suffix.lower() != ".pdf":
        messagebox.showerror("Not a PDF", "Please select a PDF file.", parent=root)
        root.destroy()
        raise SystemExit("The selected file is not a PDF.")

    try:
        document = fitz.open(selected_pdf)
    except (OSError, RuntimeError) as exc:
        messagebox.showerror("Unable to open PDF", f"Could not open the PDF:\n{exc}", parent=root)
        root.destroy()
        raise SystemExit(1) from exc

    if document.page_count == 0:
        document.close()
        messagebox.showerror("Empty PDF", "The selected PDF has no pages.", parent=root)
        root.destroy()
        raise SystemExit(1)

    root.title(f"Scan drawing dimensions - {selected_pdf.name}")
    root.geometry("1100x800")
    root.option_add("*Font", "TkDefaultFont 12")
    root.deiconify()

    style = ttk.Style(root)
    style.configure("TLabel", font=("TkDefaultFont", 12))
    style.configure("TButton", font=("TkDefaultFont", 12))
    style.configure("TFrame", font=("TkDefaultFont", 12))

    instructions = ttk.Label(
        root,
        text="Drag to select an area containing printed dimensions, then choose Scan selected area.",
        padding=8,
    )
    instructions.pack(fill="x")

    toolbar = ttk.Frame(root, padding=(8, 0, 8, 8))
    toolbar.pack(fill="x")
    page_label = ttk.Label(toolbar)
    page_label.pack(side="left", padx=(0, 12))

    canvas_frame = ttk.Frame(root)
    canvas_frame.pack(fill="both", expand=True, padx=8)
    canvas = tk.Canvas(canvas_frame, background="#707070", cursor="crosshair")
    x_scroll = ttk.Scrollbar(canvas_frame, orient="horizontal", command=canvas.xview)
    y_scroll = ttk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
    canvas.configure(xscrollcommand=x_scroll.set, yscrollcommand=y_scroll.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    y_scroll.grid(row=0, column=1, sticky="ns")
    x_scroll.grid(row=1, column=0, sticky="ew")
    canvas_frame.rowconfigure(0, weight=1)
    canvas_frame.columnconfigure(0, weight=1)

    selection: list[float] = []
    selection_id: int | None = None
    current_page = 0
    page_image = None
    photo_image = None
    ocr_engine = None

    def show_page(page_number: int) -> None:
        nonlocal current_page, page_image, photo_image, selection_id
        current_page = page_number
        page = document.load_page(current_page)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(RENDER_SCALE, RENDER_SCALE), alpha=False)
        page_image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        photo_image = ImageTk.PhotoImage(page_image)
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=photo_image)
        canvas.configure(scrollregion=(0, 0, pixmap.width, pixmap.height))
        page_label.configure(text=f"Page {current_page + 1} of {document.page_count}")
        selection.clear()
        selection_id = None
        scan_button.configure(state="disabled")
        canvas.xview_moveto(0)
        canvas.yview_moveto(0)

    def previous_page() -> None:
        if current_page > 0:
            show_page(current_page - 1)

    def next_page() -> None:
        if current_page + 1 < document.page_count:
            show_page(current_page + 1)

    def begin_selection(event: tk.Event) -> None:
        nonlocal selection_id
        selection[:] = [canvas.canvasx(event.x), canvas.canvasy(event.y)]
        if selection_id is not None:
            canvas.delete(selection_id)
        selection_id = canvas.create_rectangle(
            selection[0],
            selection[1],
            selection[0],
            selection[1],
            outline="#ff3333",
            width=3,
        )
        scan_button.configure(state="disabled")

    def update_selection(event: tk.Event) -> None:
        if selection_id is None:
            return
        x2, y2 = canvas.canvasx(event.x), canvas.canvasy(event.y)
        canvas.coords(selection_id, selection[0], selection[1], x2, y2)

    def finish_selection(event: tk.Event) -> None:
        if selection_id is None:
            return
        x2, y2 = canvas.canvasx(event.x), canvas.canvasy(event.y)
        selection[:] = [selection[0], selection[1], x2, y2]
        if abs(x2 - selection[0]) >= 5 and abs(y2 - selection[1]) >= 5:
            scan_button.configure(state="normal")

    def scan_selected_area() -> None:
        nonlocal ocr_engine
        if page_image is None or len(selection) != 4:
            return

        left, right = sorted((selection[0], selection[2]))
        top, bottom = sorted((selection[1], selection[3]))
        bounds = (
            max(0, int(left)),
            max(0, int(top)),
            min(page_image.width, int(right)),
            min(page_image.height, int(bottom)),
        )
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            messagebox.showwarning("No area selected", "Select an area inside the PDF page.", parent=root)
            return

        status_text = "Loading PaddleOCR..." if ocr_engine is None else "Scanning..."
        scan_button.configure(state="disabled", text=status_text)
        root.update_idletasks()
        try:
            if ocr_engine is None:
                from paddleocr import PaddleOCR

                ocr_engine = PaddleOCR(
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    engine="paddle",
                )

            with tempfile.TemporaryDirectory() as temporary_directory:
                crop_path = Path(temporary_directory) / "selected-area.png"
                page_image.crop(bounds).save(crop_path)
                predictions = ocr_engine.predict(str(crop_path))

            recognized_lines = []
            for prediction in predictions:
                payload = prediction.json
                result_data = payload.get("res", payload)
                recognized_lines.extend(
                    text for text in result_data.get("rec_texts", []) if text
                )
            recognized_text = "\n".join(recognized_lines).strip()
        except ImportError as exc:
            messagebox.showerror(
                "PaddleOCR is not installed",
                "Install PaddleOCR in a Python 3.12 environment, then run this program "
                "with that environment's interpreter.\n\n"
                "In PowerShell:\n"
                "py -3.12 -m venv .venv-paddle\n"
                ".\\.venv-paddle\\Scripts\\python.exe -m pip install --upgrade pip\n"
                ".\\.venv-paddle\\Scripts\\python.exe -m pip install paddlepaddle paddleocr\n"
                ".\\.venv-paddle\\Scripts\\python.exe -m pip install pymupdf pillow\n\n"
                f"Current interpreter: {sys.executable}\n\nDetails: {exc}",
                parent=root,
            )
            return
        except (OSError, RuntimeError, ValueError) as exc:
            messagebox.showerror("OCR failed", f"Could not scan the selected area:\n{exc}", parent=root)
            return
        finally:
            scan_button.configure(state="normal", text="Scan selected area")

        dimensions = list(dict.fromkeys(match.strip() for match in DIMENSION_PATTERN.findall(recognized_text)))
        if dimensions:
            result = "Detected dimensions:\n" + "\n".join(f"• {dimension}" for dimension in dimensions)
        else:
            result = "No dimension-like values were detected."
        if recognized_text:
            result += f"\n\nOCR text:\n{recognized_text}"
        else:
            result += "\n\nNo text was recognized in the selected area."
        messagebox.showinfo("Scan result", result, parent=root)

    previous_button = ttk.Button(toolbar, text="Previous page", command=previous_page)
    previous_button.pack(side="left", padx=3)
    next_button = ttk.Button(toolbar, text="Next page", command=next_page)
    next_button.pack(side="left", padx=3)
    scan_button = ttk.Button(
        toolbar,
        text="Scan selected area",
        command=scan_selected_area,
        state="disabled",
    )
    scan_button.pack(side="right", padx=3)

    canvas.bind("<ButtonPress-1>", begin_selection)
    canvas.bind("<B1-Motion>", update_selection)
    canvas.bind("<ButtonRelease-1>", finish_selection)
    root.protocol("WM_DELETE_WINDOW", lambda: (document.close(), root.destroy()))

    try:
        show_page(0)
        root.mainloop()
    finally:
        if not document.is_closed:
            document.close()


if __name__ == "__main__":
    main()