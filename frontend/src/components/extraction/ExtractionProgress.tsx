import { Loader2, CheckCircle2 } from "lucide-react";

interface Props {
  status: "idle" | "extracting" | "done" | "error";
  progress: number;
  message: string;
}

const steps = [
  "Reading PDF pages",
  "Identifying key sections",
  "Extracting deal info",
  "Extracting certificate classes",
  "Extracting fees & waterfall rules",
  "Assembling deal configuration",
];

export default function ExtractionProgress({ status, progress, message }: Props) {
  if (status === "idle") return null;

  const currentStep = Math.floor((progress / 100) * steps.length);

  return (
    <div className="card fade-in">
      <div className="flex items-center gap-3 mb-4">
        {status === "extracting" ? (
          <Loader2 size={20} className="text-primary-600 animate-spin" />
        ) : status === "done" ? (
          <CheckCircle2 size={20} className="text-green-600" />
        ) : (
          <div className="w-5 h-5 rounded-full bg-red-100 flex items-center justify-center">
            <span className="text-red-600 text-xs font-bold">!</span>
          </div>
        )}
        <p className="text-sm font-semibold text-gray-800">{message}</p>
        <span className="ml-auto text-xs font-bold text-primary-700">{progress}%</span>
      </div>

      {/* Progress bar */}
      <div className="w-full h-2 bg-gray-100 rounded-full overflow-hidden mb-5">
        <div
          className="h-full bg-gradient-to-r from-primary-600 to-primary-400 rounded-full transition-all duration-500 progress-pulse"
          style={{ width: `${progress}%` }}
        />
      </div>

      {/* Steps */}
      <div className="space-y-2">
        {steps.map((step, i) => {
          const done = i < currentStep;
          const active = i === currentStep && status === "extracting";
          return (
            <div key={step} className="flex items-center gap-2.5">
              <div
                className={`w-5 h-5 rounded-full flex items-center justify-center flex-shrink-0 text-xs font-bold
                  ${done ? "bg-primary-700 text-white" : active ? "bg-primary-100 text-primary-700 border-2 border-primary-500" : "bg-gray-100 text-gray-400"}`}
              >
                {done ? "✓" : i + 1}
              </div>
              <span className={`text-xs ${done ? "text-gray-700 font-medium" : active ? "text-primary-700 font-semibold" : "text-gray-400"}`}>
                {step}
              </span>
              {active && <Loader2 size={12} className="text-primary-500 animate-spin ml-auto" />}
            </div>
          );
        })}
      </div>
    </div>
  );
}
