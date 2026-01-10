import React, { useEffect, useMemo, useState } from "react";

// UI components
import AnalyticsDashboard from "./components/AnalyticsDashboard.jsx";
import KanbanColumn from "./components/KanbanColumn.jsx";
import OpportunityDrawer from "./components/OpportunityDrawer.jsx";

// Icons
import { LogOut } from "lucide-react";

/**
 * Backend API
 * IMPORTANT:
 * - Use relative "/api" so Nginx can proxy to backend container
 */
const BACKEND_API = import.meta.env.VITE_BACKEND_API || "/api";

/**
 * Kanban stages (single source of truth)
 */
const KANBAN_STAGES = [
  { id: "APPLIED", name: "Applied", color: "bg-indigo-600" },
  { id: "HR_SCREEN", name: "HR Screen", color: "bg-blue-600" },
  { id: "FIRST_INT", name: "1st Interview", color: "bg-cyan-600" },
  { id: "TECH_INT", name: "Tech Interview", color: "bg-amber-500" },
  { id: "HM_INT", name: "Hiring Manager", color: "bg-purple-600" },
  { id: "PANEL_INT", name: "Panel", color: "bg-pink-600" },
  { id: "OFFER", name: "Offer", color: "bg-green-600" },
  { id: "REJECTED", name: "Rejected", color: "bg-red-600" },
];

/**
 * Demo data (renders even if backend is empty)
 */
const DEMO_JOBS = [
  {
    ID: "TID-001",
    Company: "Novartis",
    Role: "Senior Security Architect",
    Status: "HR_SCREEN",
    Stage: "HR_SCREEN",
    HRContact: "recruiter@novartis.com",
    NextInterview: "2026-01-15 10:00",
    Notes: "Initial recruiter screen",
  },
];

export default function App() {
  const [jobs, setJobs] = useState(DEMO_JOBS);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [selectedJob, setSelectedJob] = useState(null);

  /**
   * Fetch jobs from backend
   */
  useEffect(() => {
    setLoading(true);

    fetch(`${BACKEND_API}/jobs`)
      .then((res) => {
        if (!res.ok) throw new Error("Backend not reachable");
        return res.json();
      })
      .then((data) => {
        if (Array.isArray(data) && data.length > 0) {
          setJobs(data);
        }
      })
      .catch(() => {
        // Backend not ready → stay in demo mode
        console.warn("Backend not available, using demo data");
      })
      .finally(() => setLoading(false));
  }, []);

  /**
   * Group jobs by status for Kanban
   */
  const groupedJobs = useMemo(() => {
    return KANBAN_STAGES.reduce((acc, stage) => {
      acc[stage.id] = jobs.filter((j) => j.Status === stage.id);
      return acc;
    }, {});
  }, [jobs]);

  return (
    <div className="min-h-screen bg-[#0c0e12] text-gray-200">
      {/* HEADER */}
      <header className="bg-gray-800 border-b border-gray-700 px-6 py-4 flex justify-between items-center">
        <h1 className="text-2xl font-extrabold text-transparent bg-clip-text bg-gradient-to-r from-teal-400 to-blue-500">
          JobHuntWOW
        </h1>

        <button
          className="flex items-center gap-2 text-sm text-red-400 hover:text-red-300"
          title="Logout (local only)"
        >
          <LogOut className="w-4 h-4" />
          Logout
        </button>
      </header>

      {/* CONTENT */}
      <main className="max-w-7xl mx-auto px-4 py-8">
        {error && (
          <div className="mb-4 bg-red-800/60 text-red-100 p-3 rounded">
            {error}
          </div>
        )}

        {/* ANALYTICS */}
        <section className="mb-10">
          {loading ? (
            <div className="text-center text-gray-400">Loading pipeline…</div>
          ) : (
            <AnalyticsDashboard jobs={jobs} />
          )}
        </section>

        {/* KANBAN */}
        <section>
          <h2 className="text-2xl font-bold mb-4">Interview Pipeline</h2>

          <div className="flex gap-6 overflow-x-auto pb-4">
            {KANBAN_STAGES.map((stage) => (
              <KanbanColumn
                key={stage.id}
                stage={stage}
                jobs={groupedJobs[stage.id] || []}
                onCardClick={(job) => setSelectedJob(job)}
              />
            ))}
          </div>
        </section>
      </main>

      {/* DRAWER */}
      {selectedJob && (
        <OpportunityDrawer
          job={selectedJob}
          onClose={() => setSelectedJob(null)}
        />
      )}

      {/* FOOTER */}
      <footer className="text-center text-xs text-gray-500 py-6 border-t border-gray-800 mt-12">
        JobHuntWOW · Local Docker Edition · v1.0
      </footer>
    </div>
  );
}
