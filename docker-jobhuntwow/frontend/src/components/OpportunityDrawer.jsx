import React, { useState } from 'react';
import { X, Star, Mail, Trash2, DollarSign, MapPin, User, Calendar, Clipboard, Link, BarChart } from 'lucide-react';
import OpportunityProgressBar from './OpportunityProgressBar';
import OpportunityTimeline from './OpportunityTimeline';

const OpportunityDrawer = ({ job, onClose }) => { 
    const [activeTab, setActiveTab] = useState('Activity');
    const companyLogoUrl = `https://logo.clearbit.com/${job.Company.toLowerCase().replace(/[^a-z0-9.]/g, '')}.com?size=50`;

    const SummaryItem = ({ icon: Icon, label, value }) => (
        <div className="flex items-start space-x-3 p-3 bg-gray-700/50 rounded-lg">
            <Icon className="w-5 h-5 text-teal-400 mt-1" />
            <div>
                <p className="text-xs text-gray-400">{label}</p>
                <p className="text-sm font-medium text-white break-words">{value}</p>
            </div>
        </div>
    );

    return (
        <div className="fixed inset-0 z-50 overflow-hidden">
            <div className="absolute inset-0 bg-black/50" onClick={onClose}></div>
            <div className="fixed inset-y-0 right-0 max-w-full flex">
                <div className="w-screen max-w-3xl bg-gray-900 border-l border-gray-700 shadow-2xl overflow-y-auto">
                    <div className="sticky top-0 bg-gray-900 z-10 p-6 border-b border-gray-800">
                        <div className="flex justify-between items-center mb-4">
                            <div className="flex items-center space-x-4">
                                <img src={companyLogoUrl} alt="" className="w-10 h-10 rounded-lg bg-white p-1" 
                                     onError={(e) => { e.target.src="https://placehold.co/50x50/374151/f9fafb?text=Co"; }} />
                                <div>
                                    <h2 className="text-2xl font-bold text-white">{job.Company}</h2>
                                    <p className="text-md text-gray-400">{job.Role}</p>
                                </div>
                            </div>
                            <button onClick={onClose} className="p-2 rounded-full text-white bg-blue-600 hover:bg-blue-500">
                                <X className="w-6 h-6" />
                            </button>
                        </div>
                        <OpportunityProgressBar currentStage={job.Stage} />
                    </div>

                    <div className="p-6 space-y-8">
                        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                            <SummaryItem icon={DollarSign} label="Salary" value={job.SalaryExpectation || 'N/A'} />
                            <SummaryItem icon={MapPin} label="Location" value={job.Location || 'N/A'} />
                            <SummaryItem icon={User} label="HR Contact" value={job.HRContact || 'N/A'} />
                        </div>

                        <div className="bg-gray-800/50 p-4 rounded-xl border border-gray-700">
                            <h3 className="text-lg font-bold text-white mb-3 flex items-center">
                                <BarChart className="w-5 h-5 mr-2 text-teal-400" /> AI Insights
                            </h3>
                            <p className="text-sm text-gray-300">Probability: <span className="text-green-400">{job.Insights?.probability || 'N/A'}</span></p>
                        </div>

                        <div className="mt-4 p-4 bg-gray-800/50 rounded-xl">
                            <OpportunityTimeline activities={job.ActivityFeed || []} />
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
};

export default OpportunityDrawer;