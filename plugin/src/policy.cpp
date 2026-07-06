#include "policy.h"

#include <cstdlib>
#include <iostream>
#include <stdexcept>

void POLICY::addPolicies()
{
    CGSim::PolicyManager::addPolicy(storage_rebalance_policy());
    CGSim::PolicyManager::addPolicy(network_aware_rebalance_policy());
    CGSim::PolicyManager::addPolicy(hotset_replication_policy());
}

CGSim::Policy* POLICY::storage_rebalance_policy()
{
    auto* p = new CGSim::Policy();

    p->start_time = 0.0;
    p->end_time = 0.0;
    p->repeat_interval = 1000.0;
    p->name = "Storage Rebalance Policy";

    const std::string policy_name = p->name;

    p->callback = [this, policy_name]() {
        run_storage_rebalance(
            policy_name,
            0.10,   // high_utilization_threshold
            0.10,   // low_utilization_threshold
            1,      // max_transfers_per_tick
            FilePickMode::FIRST_FIT,
            true,   // skip_if_already_replica_on_destination
            CGSim::FileTransferDecisionMode::COPY
        );
    };

    return p;
}

CGSim::Policy* POLICY::network_aware_rebalance_policy()
{
    auto* p = new CGSim::Policy();

    p->start_time = 10000.0;
    p->end_time = 8000000.0;
    p->repeat_interval = 1000.0;
    p->name = "Network Aware Rebalance Policy";

    const std::string policy_name = p->name;

    p->callback = [this, policy_name]() {
        run_network_aware_rebalance(
            policy_name,
            0.10,   // high_utilization_threshold
            0.10,   // low_utilization_threshold
            0.10,   // max_path_load
            1,      // max_transfers_per_tick
            FilePickMode::FIRST_FIT,
            PathMetricMode::ESTIMATED_TRANSFER_TIME,
            CGSim::FileTransferDecisionMode::COPY
        );
    };

    return p;
}

CGSim::Policy* POLICY::hotset_replication_policy()
{
    auto* p = new CGSim::Policy();

    p->start_time = 0.0;
    p->end_time = 0.0;
    p->repeat_interval = 10000.0;
    p->name = "Hotset Replication Policy";

    const std::string policy_name = p->name;

    p->callback = [this, policy_name]() {
        run_hotset_replication(
            policy_name,
            0.00070,   // hotness_threshold, implemented as replica prevalence
            3,      // target_replica_count
            1,      // max_transfers_per_tick
            CGSim::FileTransferDecisionMode::COPY
        );
    };

    return p;
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

