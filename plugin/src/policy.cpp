#include "policy.h"

#include <cstdlib>
#include <iostream>
#include <stdexcept>

void POLICY::configurePolicy(const std::string& policy_file)
{
    // read the policy config file
    std::ifstream file(policy_file);
    file >> policy_content; // read the policy content into json_data
    file.close();

    // check if the proactive policy is enabled
    if(policy_content["Data_Management_Policy"]["proactive"]["enabled"] != true){
        return;
    }

    // get the interval of periodic proactive policy
    float interval          = policy_content["Data_Management_Policy"]["proactive"]["interval"];
    // 0 as COPY, 1 as MOVE, TODO: may need to add error handling for invalid data transfer mode
    bool data_transfer_mode = (policy_content["Data_Management_Policy"]["proactive"]["data_transfer_mode"] == "COPY")?false:true;

    int template_index = int(policy_content["Data_Management_Policy"]["proactive"]["transfer_template"][0]);

    // get the storage rebalance policy (choose from list of policies)
    if(template_index == 0){
        float high_utilization_threshold = policy_content["Data_Management_Policy"]["proactive"]["template_params"]["storage_rebalance"]["high_utilization_threshold"];
        float low_utilization_threshold = policy_content["Data_Management_Policy"]["proactive"]["template_params"]["storage_rebalance"]["low_utilization_threshold"];
        int max_transfers_per_tick = policy_content["Data_Management_Policy"]["proactive"]["template_params"]["storage_rebalance"]["max_transfers_per_tick"];
        FilePickMode file_pick = (policy_content["Data_Management_Policy"]["proactive"]["template_params"]["storage_rebalance"]["file_pick"][0] == 0)?FilePickMode::FIRST_FIT:(
            policy_content["Data_Management_Policy"]["proactive"]["template_params"]["storage_rebalance"]["file_pick"][0] == 1)?FilePickMode::LARGEST_FIT:(
                policy_content["Data_Management_Policy"]["proactive"]["template_params"]["storage_rebalance"]["file_pick"][0] == 2)?FilePickMode::SMALLEST_FIT:FilePickMode::RANDOM_FIT;
        bool skip_if_already_replica_on_destination = 
            (policy_content["Data_Management_Policy"]["proactive"]["template_params"]["storage_rebalance"]["skip_if_already_replica_on_destination"] == true)?true:false;
        CGSim::FileTransferDecisionMode mode = (data_transfer_mode)?CGSim::FileTransferDecisionMode::MOVE:CGSim::FileTransferDecisionMode::COPY;
        CGSim::PolicyManager::addPolicy(storage_rebalance_policy(interval,
                                                                high_utilization_threshold, 
                                                                low_utilization_threshold, 
                                                                max_transfers_per_tick, 
                                                                file_pick, 
                                                                skip_if_already_replica_on_destination, 
                                                                mode));
    }
    else if(template_index == 1){
        double high_utilization_threshold = policy_content["Data_Management_Policy"]["proactive"]["template_params"]["network_aware_rebalance"]["high_utilization_threshold"];
        double low_utilization_threshold = policy_content["Data_Management_Policy"]["proactive"]["template_params"]["network_aware_rebalance"]["low_utilization_threshold"];
        double max_path_load = policy_content["Data_Management_Policy"]["proactive"]["template_params"]["network_aware_rebalance"]["max_path_load"];
        int max_transfers_per_tick = policy_content["Data_Management_Policy"]["proactive"]["template_params"]["network_aware_rebalance"]["max_transfers_per_tick"];
        FilePickMode file_pick = (policy_content["Data_Management_Policy"]["proactive"]["template_params"]["network_aware_rebalance"]["file_pick"][0] == 0)?FilePickMode::FIRST_FIT:(
            policy_content["Data_Management_Policy"]["proactive"]["template_params"]["network_aware_rebalance"]["file_pick"][0] == 1)?FilePickMode::LARGEST_FIT:(
                policy_content["Data_Management_Policy"]["proactive"]["template_params"]["network_aware_rebalance"]["file_pick"][0] == 2)?FilePickMode::SMALLEST_FIT:FilePickMode::RANDOM_FIT;
        PathMetricMode path_metric = (policy_content["Data_Management_Policy"]["proactive"]["template_params"]["network_aware_rebalance"]["path_metric"][0] == 0)?PathMetricMode::LINK_LOAD:(
            policy_content["Data_Management_Policy"]["proactive"]["template_params"]["network_aware_rebalance"]["path_metric"][0] == 1)?PathMetricMode::BANDWIDTH_ONLY:PathMetricMode::ESTIMATED_TRANSFER_TIME;
        CGSim::FileTransferDecisionMode mode = (data_transfer_mode)?CGSim::FileTransferDecisionMode::MOVE:CGSim::FileTransferDecisionMode::COPY;
        CGSim::PolicyManager::addPolicy(network_aware_rebalance_policy(interval,
                                                                        high_utilization_threshold,
                                                                        low_utilization_threshold,
                                                                        max_path_load,
                                                                        max_transfers_per_tick,
                                                                        file_pick,
                                                                        path_metric,
                                                                        mode));
    }
    else if(template_index == 2){
        float hotness_window = policy_content["Data_Management_Policy"]["proactive"]["template_params"]["hotset_replication"]["hotness_window"];
        double hotness_threshold = policy_content["Data_Management_Policy"]["proactive"]["template_params"]["hotset_replication"]["hotness_threshold"];
        float prediction_horizon = policy_content["Data_Management_Policy"]["proactive"]["template_params"]["hotset_replication"]["prediction_horizon"];
        int target_replica_count = policy_content["Data_Management_Policy"]["proactive"]["template_params"]["hotset_replication"]["target_replica_count"];
        CandidateDestinationPolicy candidate_destination_policy = 
            (policy_content["Data_Management_Policy"]["proactive"]["template_params"]["hotset_replication"]["candidate_destination_policy"][0] == 0)?CandidateDestinationPolicy::REQUESTING_SITES_FIRST:CandidateDestinationPolicy::LEAST_UTILIZED_AMONG_REQUESTING;
        int max_transfers_per_tick = policy_content["Data_Management_Policy"]["proactive"]["template_params"]["hotset_replication"]["max_transfers_per_tick"];
        CGSim::FileTransferDecisionMode mode = (data_transfer_mode)?CGSim::FileTransferDecisionMode::MOVE:CGSim::FileTransferDecisionMode::COPY;
        CGSim::PolicyManager::addPolicy(hotset_replication_policy(interval,
                                                                    hotness_window,
                                                                    hotness_threshold,
                                                                    prediction_horizon,
                                                                    target_replica_count,
                                                                    candidate_destination_policy,
                                                                    max_transfers_per_tick,
                                                                    mode));
    }
    else if(template_index == 3){
        CGSim::PolicyManager::addPolicy(custom_policy_agent_policy());
    }
    else{
        std::cerr << "Invalid transfer template:" << template_index << std::endl;
        throw std::runtime_error("Invalid transfer template");
        return;
    }
}

CGSim::Policy* POLICY::storage_rebalance_policy(float interval,
                                                double high_utilization_threshold, 
                                                double low_utilization_threshold, 
                                                int max_transfers_per_tick, 
                                                FilePickMode file_pick, 
                                                bool skip_if_already_replica_on_destination, 
                                                CGSim::FileTransferDecisionMode mode)
{
    auto* p = new CGSim::Policy();

    p->start_time = 0.0;
    p->end_time = 0.0;
    p->repeat_interval = interval;
    p->name = "Storage Rebalance Policy";

    const std::string policy_name = p->name;

    p->callback = [this, policy_name, high_utilization_threshold, low_utilization_threshold,
                   max_transfers_per_tick, file_pick, skip_if_already_replica_on_destination, mode]() {
        run_storage_rebalance(
            policy_name,
            high_utilization_threshold,
            low_utilization_threshold,
            max_transfers_per_tick,
            file_pick,
            skip_if_already_replica_on_destination,
            mode
        );
    };

    return p;
}

CGSim::Policy* POLICY::network_aware_rebalance_policy(float interval,
                                                    double high_utilization_threshold, 
                                                    double low_utilization_threshold, 
                                                    double max_path_load, 
                                                    int max_transfers_per_tick, 
                                                    FilePickMode file_pick, 
                                                    PathMetricMode path_metric, 
                                                    CGSim::FileTransferDecisionMode mode)
{
    auto* p = new CGSim::Policy();

    p->start_time = 10000.0;
    p->end_time = 8000000.0;
    p->repeat_interval = interval;
    p->name = "Network Aware Rebalance Policy";

    const std::string policy_name = p->name;

    p->callback = [this, policy_name, high_utilization_threshold, low_utilization_threshold,
                   max_path_load, max_transfers_per_tick, file_pick, path_metric, mode]() {
        run_network_aware_rebalance(
            policy_name,
            high_utilization_threshold,
            low_utilization_threshold,
            max_path_load,
            max_transfers_per_tick,
            file_pick,
            path_metric,
            mode
        );
    };

    return p;
}

CGSim::Policy* POLICY::hotset_replication_policy(float interval,
                                                float hotness_window,
                                                double hotness_threshold,
                                                float prediction_horizon,
                                                int target_replica_count,
                                                CandidateDestinationPolicy candidate_destination_policy,
                                                int max_transfers_per_tick,
                                                CGSim::FileTransferDecisionMode mode)
{
    auto* p = new CGSim::Policy();

    p->start_time = 0.0;
    p->end_time = 0.0;
    p->repeat_interval = interval;
    p->name = "Hotset Replication Policy";

    const std::string policy_name = p->name;

    p->callback = [this, policy_name, hotness_threshold, target_replica_count,
                   max_transfers_per_tick, mode]() {
        run_hotset_replication(
            policy_name,
            hotness_threshold,
            target_replica_count,
            max_transfers_per_tick,
            mode
        );
    };

    return p;
}

CGSim::Policy* POLICY::custom_policy_agent_policy()
{
    throw std::runtime_error("custom_policy_agent policy is not implemented");
}

void POLICY::run_storage_rebalance(
    const std::string& policy_name,
    double high_utilization_threshold,
    double low_utilization_threshold,
    int max_transfers_per_tick,
    FilePickMode file_pick,
    bool skip_if_already_replica_on_destination,
    CGSim::FileTransferDecisionMode mode)
{
    auto utils = get_site_utils();

    std::vector<SiteUtil> sources;
    std::vector<SiteUtil> destinations;

    for (const auto& s : utils) {
        if (s.util >= high_utilization_threshold) {
            sources.push_back(s);
        }
        if (s.util <= low_utilization_threshold) {
            destinations.push_back(s);
        }
    }

    std::sort(sources.begin(), sources.end(),
              [](const SiteUtil& a, const SiteUtil& b) {
                  return a.util > b.util;
              });

    std::sort(destinations.begin(), destinations.end(),
              [](const SiteUtil& a, const SiteUtil& b) {
                  return a.util < b.util;
              });

    int transfers_started = 0;

    for (const auto& src : sources) {
        for (const auto& dst : destinations) {
            if (src.site == dst.site) {
                continue;
            }

            std::string filename;
            unsigned long long filesize = 0;

            if (!choose_file_from_site(
                    src.site,
                    dst.site,
                    dst.remaining,
                    file_pick,
                    skip_if_already_replica_on_destination,
                    filename,
                    filesize)) {
                continue;
            }

            if (CGSim::get_file_manager()->is_in_flight(filename, src.site, dst.site)) continue;
            CGSim::get_file_manager()->make_background_transfer(filename, src.site, dst.site, mode,policy_name);

            transfers_started++;
            if (transfers_started >= max_transfers_per_tick) {
                return;
            }
        }
    }
}

void POLICY::run_network_aware_rebalance(
    const std::string& policy_name,
    double high_utilization_threshold,
    double low_utilization_threshold,
    double max_path_load,
    int max_transfers_per_tick,
    FilePickMode file_pick,
    PathMetricMode path_metric,
    CGSim::FileTransferDecisionMode mode)
{
    auto utils = get_site_utils();

    std::vector<SiteUtil> sources;
    std::vector<SiteUtil> destinations;

    for (const auto& s : utils) {
        if (s.util >= high_utilization_threshold) {
            sources.push_back(s);
        }
        if (s.util <= low_utilization_threshold) {
            destinations.push_back(s);
        }
    }

    std::vector<Candidate> candidates;

    for (const auto& src : sources) {
        for (const auto& dst : destinations) {
            if (src.site == dst.site) {
                continue;
            }

            auto* link = link_between_sites(src.site, dst.site);
            if (!link) {
                continue;
            }

            const double load = link->get_load();
            if (load > max_path_load) {
                continue;
            }

            std::string filename;
            unsigned long long filesize = 0;

            if (!choose_file_from_site(
                    src.site,
                    dst.site,
                    dst.remaining,
                    file_pick,
                    true,
                    filename,
                    filesize)) {
                continue;
            }

            if (CGSim::get_file_manager()->is_in_flight(filename, src.site, dst.site)) {
                continue;
            }

            const double bandwidth = link->get_bandwidth();
            const double latency = link->get_latency();

            if (bandwidth <= 0.0) {
                continue;
            }

            Candidate c;
            c.filename = filename;
            c.src_site = src.site;
            c.dst_site = dst.site;
            c.size = filesize;
            c.link_load = load;
            c.bandwidth = bandwidth;
            c.latency = latency;
            c.estimated_time = latency + static_cast<double>(filesize) / bandwidth;

            candidates.push_back(c);
        }
    }

    auto better = [path_metric](const Candidate& a, const Candidate& b) {
        switch (path_metric) {
            case PathMetricMode::LINK_LOAD:
                if (a.link_load != b.link_load) {
                    return a.link_load < b.link_load;
                }
                if (a.estimated_time != b.estimated_time) {
                    return a.estimated_time < b.estimated_time;
                }
                return a.bandwidth > b.bandwidth;

            case PathMetricMode::BANDWIDTH_ONLY:
                if (a.bandwidth != b.bandwidth) {
                    return a.bandwidth > b.bandwidth;
                }
                if (a.estimated_time != b.estimated_time) {
                    return a.estimated_time < b.estimated_time;
                }
                return a.link_load < b.link_load;

            case PathMetricMode::ESTIMATED_TRANSFER_TIME:
            default:
                if (a.estimated_time != b.estimated_time) {
                    return a.estimated_time < b.estimated_time;
                }
                if (a.link_load != b.link_load) {
                    return a.link_load < b.link_load;
                }
                return a.bandwidth > b.bandwidth;
        }
    };

    std::sort(candidates.begin(), candidates.end(), better);

    int transfers_started = 0;

    for (const auto& c : candidates) {
        if (CGSim::get_file_manager()->is_in_flight(c.filename, c.src_site, c.dst_site)) {
            continue;
        }

        CGSim::get_file_manager()->make_background_transfer(c.filename, c.src_site, c.dst_site, mode,policy_name);


        transfers_started++;
        if (transfers_started >= max_transfers_per_tick) {
            return;
        }
    }
}

void POLICY::run_hotset_replication(
    const std::string& policy_name,
    double hotness_threshold,
    int target_replica_count,
    int max_transfers_per_tick,
    CGSim::FileTransferDecisionMode mode)
{
    auto sites = CGSim::get_site_manager()->get_all_sites();
    const std::size_t site_count = sites.size();

    if (site_count == 0) {
        return;
    }

    auto utils = get_site_utils();

    std::unordered_map<std::string, SiteUtil> util_by_site;
    for (const auto& u : utils) {
        util_by_site[u.site] = u;
    }

    std::unordered_map<std::string, std::unordered_set<std::string>> file_to_sites;

    for (const auto& site : sites) {
        auto files = CGSim::get_file_manager()->request_site_files(site);
        for (const auto& filename : files) {
            file_to_sites[filename].insert(site);
        }
    }

    struct HotCandidate {
        std::string filename;
        std::unordered_set<std::string> replicas;
        double prevalence = 0.0;
    };

    std::vector<HotCandidate> hot_files;

    for (const auto& [filename, replica_sites] : file_to_sites) {
        const int replica_count = static_cast<int>(replica_sites.size());

        if (replica_count >= target_replica_count) {
            continue;
        }

        const double prevalence =
            static_cast<double>(replica_count) / static_cast<double>(site_count);

        if (prevalence < hotness_threshold) {
            continue;
        }

        hot_files.push_back({filename, replica_sites, prevalence});
    }

    std::sort(hot_files.begin(), hot_files.end(),
              [](const HotCandidate& a, const HotCandidate& b) {
                  return a.prevalence > b.prevalence;
              });

    int transfers_started = 0;

    for (const auto& hot : hot_files) {
        const unsigned long long filesize =
            CGSim::get_file_manager()->request_file_size(hot.filename);

        std::vector<std::string> src_candidates;
        for (const auto& site : hot.replicas) {
            src_candidates.push_back(site);
        }

        std::sort(src_candidates.begin(), src_candidates.end(),
                  [&util_by_site](const std::string& a, const std::string& b) {
                      return util_by_site[a].util > util_by_site[b].util;
                  });

        std::vector<SiteUtil> dst_candidates;
        for (const auto& u : utils) {
            if (hot.replicas.find(u.site) == hot.replicas.end() &&
                u.remaining >= filesize) {
                dst_candidates.push_back(u);
            }
        }

        std::sort(dst_candidates.begin(), dst_candidates.end(),
                  [](const SiteUtil& a, const SiteUtil& b) {
                      return a.util < b.util;
                  });

        for (const auto& src_site : src_candidates) {
            for (const auto& dst : dst_candidates) {
                if (CGSim::get_file_manager()->is_in_flight(hot.filename, src_site, dst.site)) {
                    continue;
                }

                CGSim::get_file_manager()->make_background_transfer(hot.filename, src_site, dst.site, mode,policy_name);


                transfers_started++;
                if (transfers_started >= max_transfers_per_tick) {
                    return;
                }
            }
        }
    }
}

std::vector<POLICY::SiteUtil> POLICY::get_site_utils() const
{
    std::vector<SiteUtil> out;

    auto sites = CGSim::get_site_manager()->get_all_sites();

    for (const auto& site_name : sites) {
        auto* zone = sg4::Engine::get_instance()->netzone_by_name_or_null(site_name);
        if (!zone) {
            continue;
        }

        const std::string cap_prop = zone->get_property("storage_capacity_bytes");
        if (cap_prop.empty()) {
            continue;
        }

        const unsigned long long capacity = std::stoull(cap_prop);
        if (capacity == 0) {
            continue;
        }

        const unsigned long long remaining =
            CGSim::get_file_manager()->request_remaining_site_storage(site_name);

        SiteUtil u;
        u.site = site_name;
        u.remaining = remaining;
        u.util = 1.0 - static_cast<double>(remaining) / static_cast<double>(capacity);

        out.push_back(u);
    }

    return out;
}

bool POLICY::choose_file_from_site(
    const std::string& src_site,
    const std::string& dst_site,
    unsigned long long dst_remaining,
    FilePickMode file_pick,
    bool skip_if_already_replica_on_destination,
    std::string& filename,
    unsigned long long& filesize)
{
    auto files = CGSim::get_file_manager()->request_site_files(src_site);
    if (files.empty()) {
        return false;
    }

    std::unordered_set<std::string> dst_files;
    if (skip_if_already_replica_on_destination) {
        dst_files = CGSim::get_file_manager()->request_site_files(dst_site);
    }

    struct FileCandidate {
        std::string name;
        unsigned long long size = 0;
    };

    std::vector<FileCandidate> candidates;

    for (const auto& f : files) {
        if (skip_if_already_replica_on_destination &&
            dst_files.find(f) != dst_files.end()) {
            continue;
        }

        const unsigned long long size =
            CGSim::get_file_manager()->request_file_size(f);

        if (size <= dst_remaining) {
            candidates.push_back({f, size});
        }
    }

    if (candidates.empty()) {
        return false;
    }

    switch (file_pick) {
        case FilePickMode::LARGEST_FIT:
            std::sort(candidates.begin(), candidates.end(),
                      [](const FileCandidate& a, const FileCandidate& b) {
                          return a.size > b.size;
                      });
            break;

        case FilePickMode::SMALLEST_FIT:
            std::sort(candidates.begin(), candidates.end(),
                      [](const FileCandidate& a, const FileCandidate& b) {
                          return a.size < b.size;
                      });
            break;

        case FilePickMode::RANDOM_FIT: {
            std::uniform_int_distribution<std::size_t> dist(0, candidates.size() - 1);
            const auto& c = candidates[dist(rng)];
            filename = c.name;
            filesize = c.size;
            return true;
        }

        case FilePickMode::FIRST_FIT:
        default:
            break;
    }

    filename = candidates.front().name;
    filesize = candidates.front().size;
    return true;
}

sg4::Link* POLICY::link_between_sites(
    const std::string& src_site,
    const std::string& dst_site) const
{
    sg4::Link* link =
        sg4::Link::by_name_or_null("link_" + src_site + ":" + dst_site);

    if (!link) {
        link = sg4::Link::by_name_or_null("link_" + dst_site + ":" + src_site);
    }

    return link;
}

void POLICY::onFileRequest(Job* j, std::string filename, long long filesize, std::unordered_set<std::string> file_locations, std::string& source_site, CGSim::FileTransferDecisionMode& mode)
{
   /*
      Reactive transfer implementation, triggered by job request here.
      Requirement:
         Assign a source site to "source_site" from a selection from "file_locations"
   */

   /* If reactive policy is not enabled, use the default policy to assign a source site. */
   if(!this->policy_content["Data_Management_Policy"]["reactive"]["enabled"]){
        std::cerr << "[WARNING] In this project, data policy has to be enabled. A default policy is used. Please check the policy file." << std::endl;
        if(file_locations.find(j->comp_site) != file_locations.end()){
        source_site = j->comp_site;
        }else source_site = *(file_locations.begin());
        return;
    }

    /* If local replica is preferred, check if the local replica is available regardless of the policy. */
    if(this->policy_content["Data_Management_Policy"]["reactive"]["prefer_local_replica"] == true){
        /* Found local replica */
        if(file_locations.find(j->comp_site) != file_locations.end()){
        source_site = j->comp_site;
        return;
        }
    }
    /* ------------------------------------------------------------------------------------------------ */
    /* non-local replica situation */
    /* If there is only one replica, use it directly regardless of the policy. */
    if(file_locations.size() == 1){
        source_site = *(file_locations.begin());
        return;
    }
    /* Otherwise, select a source site considering multiple replica situation. */
    int template_index = int(this->policy_content["Data_Management_Policy"]["reactive"]["transfer_template"][0]);
    if(template_index == 0){
        /* first fit (same as default) */
        source_site = *(file_locations.begin());
        return;
    }else if(template_index == 1){
        /* least utilized source */
        auto utils = get_site_utils();
        float min_util = std::numeric_limits<float>::max();
        for(const auto& s : utils){
        if(s.util < min_util){
            min_util = s.util;
            source_site = s.site;
        }
        }
        return;
    }else if(template_index == 2){
        /* most utilized source */
        auto utils = get_site_utils();
        float max_util = std::numeric_limits<float>::min();
        for(const auto& s : utils){
        if(s.util > max_util){
            max_util = s.util;
            source_site = s.site;
        }
        }
        return;
    }else if(template_index == 3){
        /* random replica */
        std::mt19937 rng{this->policy_content["Data_Management_Policy"]["reactive"]["random_seed"]};
        std::uniform_int_distribution<int> dist(0, file_locations.size() - 1);
        auto it = file_locations.begin();
        std::advance(it, dist(rng));
        source_site = *it;
        return;
    }else if(template_index == 4){
        /* custom policy agent */
        std::cerr << "[WARNING] Custom policy agent is not implemented yet. Using default policy instead." << std::endl;
        source_site = *(file_locations.begin());
        return;
    }
}