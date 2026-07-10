#include "dispatcher.h"

double DISPATCHER::storage_needed(std::unordered_map<std::string, long long>& files) 
{
    long long sum = 0;
    for (const auto& [_, value] : files)
        sum += value;
    return sum;
}

long long DISPATCHER::storage_required_for_site(Job* j, const std::string& site)
{
    long long needed = static_cast<long long>(storage_needed(j->output_files));
    for (const auto& [name, fileinfo] : j->input_files_sizes_locations) {
        if (fileinfo.second.find(site) == fileinfo.second.end()) {
            needed += static_cast<long long>(fileinfo.first);
        }
    }
    return needed;
}

void DISPATCHER::commitStorage(Job* job, unsigned long long bytes)
{
    if (job->comp_site.empty()) return;

    auto job_it = job_reserved_.find(job->jobid);
    if (job_it == job_reserved_.end()) return;

    const unsigned long long commit = std::min(bytes, job_it->second);
    if (commit == 0) return;

    job_it->second -= commit;
    auto& site_reserved = site_reserved_[job->comp_site];
    if (commit >= site_reserved) site_reserved = 0;
    else site_reserved -= commit;

    if (job_it->second == 0) job_reserved_.erase(job_it);
}

void DISPATCHER::commitOutputWrite(Job* job, unsigned long long bytes)
{
    commitStorage(job, bytes);

    auto it = output_writes_remaining_.find(job->jobid);
    if (it == output_writes_remaining_.end()) return;

    if (--it->second > 0) return;

    output_writes_remaining_.erase(it);
    releaseJobStorage(job);
}

void DISPATCHER::releaseJobStorage(Job* job)
{
    auto it = job_reserved_.find(job->jobid);
    if (it == job_reserved_.end()) return;

    auto& site_reserved = site_reserved_[job->comp_site];
    if (it->second >= site_reserved) site_reserved = 0;
    else site_reserved -= it->second;

    job_reserved_.erase(it);
}

void DISPATCHER::findBestSite(Job* j)
{
    const auto& files = j->input_files_sizes_locations;

    std::unordered_map<std::string, std::size_t> counts;
    for (const auto& [name, file] : files) for (const auto& site : file.second) ++counts[site];

    while (!counts.empty()) 
    {
        const auto best = std::max_element(counts.begin(), counts.end(),[](const auto& a, const auto& b) {return a.second < b.second;});
        const auto required = storage_required_for_site(j, best->first);
        const auto remaining = CGSim::get_file_manager()->request_remaining_site_storage(best->first);
        const auto reserved = site_reserved_[best->first];
        if (remaining >= reserved + required) {
            j->comp_site = best->first;
            return;
        }
        counts.erase(best);
    }

    return;
}


void DISPATCHER::findAvailableCPU(Job* j)
{
    if(j->comp_site == "") return;
    auto site = sg4::Engine::get_instance()->netzone_by_name_or_null(j->comp_site);
    if (!site) return;
    auto cpus = site->get_all_hosts();

    for(const auto& cpu: cpus)
    {
        if(cpu->get_name().find("JOB-SERVER_cpu") != std::string::npos) continue;
        if(cpu->get_name().find("_communication_server") != std::string::npos) continue;
        if(cpu->extension<HostExtensions>()->get_cores_available() < j->cores) continue;

        auto disks = cpu->get_disks();
        if (disks.empty()) continue;

        auto d = disks[0];

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
  if (job->comp_host != "") {
    const auto required = storage_required_for_site(job, job->comp_site);
    site_reserved_[job->comp_site] += required;
    job_reserved_[job->jobid] = required;
    if (!job->output_files.empty()) {
      output_writes_remaining_[job->jobid] = static_cast<int>(job->output_files.size());
    }
  }
  return job;
}
