import React, { useMemo } from "react";
import { BarChart } from "lucide-react";

const KANBAN_STAGES = [
  { id: "APPLIED", name: "Applied" },
  { id: "HR_SCREEN", name: "HR Screen" },
  { id: "FIRST_INT", name: "1st Interview" },
  { id: "TECH_INT", name: "Tech Interview" },
  { id: "HM_INT", name: "Hiring Manager" },
  { id: "PANEL_INT", name: "Panel" },
  { id: "OFFER", name: "Offer" },
  { id: "REJECTED", name: "Rejected" },
];

const AnalyticsDashboard = ({ jobs = [] }) => {
  const totalJobs = jobs.length;

  const statusCounts = useMemo(() => {
    return KANBAN_STAGES.reduce((acc, stage) => {
      acc[stage.id] = jobs.filter(j => j.Status === stage.id).length;
      return acc;
    }, {});
  }, [jobs]);

  return (
    <div className="bg-gray-800 p-6 rounded-xl border border-gray-700">
      <h3 className="text-xl font-bold text-white mb-6 flex items-center">
        <BarChart className="w-6 h-6 mr-2 text-teal-400" />
        Pipeline Overview
      </h3>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="bg-gray-700 p-4 rounded-lg">
          <p className="text-sm text-gray-400">Total Applications</p>
          <p className="text-3xl font-bold text-white">{totalJobs}</p>
        </div>

        <div className="bg-gray-700 p-4 rounded-lg">
          <p className="text-sm text-gray-400">Offers</p>
          <p className="text-3xl font-bold text-green-400">
            {statusCounts.OFFER || 0}
          </p>
        </div>

        <div className="bg-gray-700 p-4 rounded-lg">
          <p className="text-sm text-gray-400">Rejected</p>
          <p className="text-3xl font-bold text-red-400">
            {statusCounts.REJECTED || 0}
          </p>
        </div>

        <div className="bg-gray-700 p-4 rounded-lg">
          <p className="text-sm text-gray-400">Active</p>
          <p className="text-3xl font-bold text-yellow-400">
            {totalJobs - (statusCounts.OFFER || 0) - (statusCounts.REJECTED || 0)}
          </p>
        </div>
      </div>
    </div>
  );
};

export default AnalyticsDashboard;
