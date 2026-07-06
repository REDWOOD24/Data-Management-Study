#include "dispatcher.h"

double DISPATCHER::storage_needed(std::unordered_map<std::string, long long>& files) 
{
    long long sum = 0;
    for (const auto& [_, value] : files)
        sum += value;
    return sum;
}

void DISPATCHER::findBestSite(Job* j)
{
    const auto& files = j->input_files_sizes_locations;
    const auto needed = storage_needed(j->output_files);

    std::unordered_map<std::string, std::size_t> counts;
    for (const auto& [name, file] : files) for (const auto& site : file.second) ++counts[site];

    while (!counts.empty()) 
    {
        const auto best = std::max_element(counts.begin(), counts.end(),[](const auto& a, const auto& b) {return a.second < b.second;});
        if (CGSim::get_file_manager()->request_remaining_site_storage(best->first) >= needed)
        {j->comp_site = best->first; return;}
        counts.erase(best);
    }

    return;
}


void DISPATCHER::findAvailableCPU(Job* j)
{
    if(j->comp_site == "") return;
    auto site = sg4::Engine::get_instance()->netzone_by_name_or_null(j->comp_site);
    auto cpus = site->get_all_hosts();

    for(const auto& cpu: cpus)
    {
        if(cpu->get_name().find("JOB-SERVER_cpu") != std::string::npos) continue;
        if(cpu->get_name().find("_communication_server") != std::string::npos) continue;
        if(cpu->extension<HostExtensions>()->get_cores_available() < j->cores) continue;

        auto d = cpu->get_disks()[0]; //Change later

        j->disk           =  d->get_name();
        j->disk_read_bw   =  d->get_read_bandwidth();
        j->disk_write_bw  =  d->get_write_bandwidth();

        j->comp_host          =  cpu->get_name();
        j->comp_host_speed    =  cpu->get_speed();

        return;
    }
}

Job* DISPATCHER::assignJob(Job* job)
{
  findBestSite(job);
  findAvailableCPU(job);
  return job;
}

