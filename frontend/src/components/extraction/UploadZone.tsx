import { useRef, useState } from "react";
import { Upload, FileText, X } from "lucide-react";

interface Props {
  file: File | null;
  onFile: (f: File | null) => void;
}

export default function UploadZone({ file, onFile }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f?.type === "application/pdf") onFile(f);
  };

  return (
    <div
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
      onClick={() => !file && inputRef.current?.click()}
      className={`relative border-2 border-dashed rounded-xl p-8 text-center transition-all duration-150 cursor-pointer
        ${dragging ? "border-primary-500 bg-primary-50" : "border-gray-300 hover:border-primary-400 hover:bg-gray-50"}
        ${file ? "cursor-default" : ""}`}
    >
      <input
        ref={inputRef}
        type="file"
        accept=".pdf"
        className="hidden"
        onChange={(e) => onFile(e.target.files?.[0] ?? null)}
      />

      {file ? (
        <div className="flex items-center justify-center gap-3">
          <div className="w-12 h-12 bg-red-50 rounded-xl flex items-center justify-center flex-shrink-0">
            <FileText size={22} className="text-red-500" />
          </div>
          <div className="text-left">
            <p className="text-sm font-semibold text-gray-800">{file.name}</p>
            <p className="text-xs text-gray-500 mt-0.5">
              {(file.size / 1024 / 1024).toFixed(2)} MB · PDF
            </p>
          </div>
          <button
            onClick={(e) => { e.stopPropagation(); onFile(null); }}
            className="ml-auto p-1.5 rounded-lg hover:bg-gray-200 text-gray-400"
          >
            <X size={16} />
          </button>
        </div>
      ) : (
        <>
          <div className="w-14 h-14 bg-primary-50 rounded-full flex items-center justify-center mx-auto mb-3">
            <Upload size={24} className="text-primary-600" />
          </div>
          <p className="text-sm font-semibold text-gray-700">
            Drop your PDF here, or <span className="text-primary-600">browse</span>
          </p>
          <p className="text-xs text-gray-400 mt-1">Supports deal indenture PDFs up to 100MB</p>
        </>
      )}
    </div>
  );
}
