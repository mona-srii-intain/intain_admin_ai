import { NavLink } from "react-router-dom";
import { FileText, Database, BarChart2, Activity, ClipboardList } from "lucide-react";

const navItems = [
  { to: "/extraction",     icon: FileText,      label: "Deal Indenture" },
  { to: "/config-review",  icon: ClipboardList, label: "Deal Config Review" },
  { to: "/loantape",       icon: Database,      label: "Loantape & Waterfall" },
  { to: "/reports",        icon: BarChart2,     label: "Reports" },
];

export default function Sidebar() {
  return (
    <aside className="fixed left-0 top-0 h-full w-16 lg:w-60 bg-primary-800 flex flex-col z-30">
      {/* Logo */}
      <div className="flex items-center gap-3 px-4 py-5 border-b border-primary-700">
        <div className="w-8 h-8 bg-white rounded-lg flex items-center justify-center flex-shrink-0">
          <Activity size={16} className="text-primary-700" />
        </div>
        <span className="hidden lg:block text-white font-bold text-lg tracking-tight">
          int<span className="text-primary-300">ai</span>n
        </span>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-2 py-4 space-y-1">
        {navItems.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              `flex items-center gap-3 px-3 py-2.5 rounded-lg transition-all duration-150 group ${
                isActive
                  ? "bg-white/15 text-white"
                  : "text-primary-200 hover:bg-white/10 hover:text-white"
              }`
            }
          >
            {({ isActive }) => (
              <>
                <Icon
                  size={18}
                  className={`flex-shrink-0 ${isActive ? "text-white" : "text-primary-300 group-hover:text-white"}`}
                />
                <span className="hidden lg:block text-sm font-medium">{label}</span>
                {/* Active indicator bar */}
                {isActive && (
                  <span className="hidden lg:block ml-auto w-1.5 h-1.5 rounded-full bg-white" />
                )}
              </>
            )}
          </NavLink>
        ))}
      </nav>

    </aside>
  );
}
