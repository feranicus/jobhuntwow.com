import React from 'react';
import KanbanCard from './KanbanCard';

const KanbanColumn = ({ stage, jobs, onCardClick, onDragStart, onDrop }) => {
    const handleDragOver = (e) => {
        e.preventDefault();
        e.currentTarget.classList.add('bg-gray-700/70', 'ring-2', 'ring-teal-400');
    };

    const handleDragLeave = (e) => {
        e.currentTarget.classList.remove('bg-gray-700/70', 'ring-2', 'ring-teal-400');
    };

    const handleOnDrop = (e) => {
        e.currentTarget.classList.remove('bg-gray-700/70', 'ring-2', 'ring-teal-400');
        onDrop(e, stage.id);
    };

    return (
        <div 
            className="flex-shrink-0 w-80 bg-gray-900/50 p-3 rounded-xl border border-gray-700/50"
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleOnDrop}
        >
            <div className={`p-2 rounded-t-lg font-bold text-white ${stage.color}`}>
                {stage.name} <span className="ml-2 text-xs bg-white text-gray-800 rounded-full px-2">{jobs.length}</span>
            </div>
            <div className="mt-3 space-y-4 min-h-[100px]">
                {jobs.map(job => (
                    <KanbanCard key={job.ID} job={job} onClick={onCardClick} onDragStart={onDragStart} />
                ))}
            </div>
        </div>
    );
};

export default KanbanColumn;