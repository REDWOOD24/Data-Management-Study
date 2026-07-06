#ifndef POLICY_H
#define POLICY_H

#include "CGSim.h"
#include "output.h"

#include <algorithm>
#include <limits>
#include <memory>
#include <random>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace sg4 = simgrid::s4u;

class POLICY {
public:
    POLICY() = default;
    ~POLICY() = default;

    void addPolicies();

private:
    enum class FilePickMode {
        FIRST_FIT,
        LARGEST_FIT,
        SMALLEST_FIT,
        RANDOM_FIT
    };

    enum class PathMetricMode {
        ESTIMATED_TRANSFER_TIME,
        LINK_LOAD,
        BANDWIDTH_ONLY
    };

    struct SiteUtil {
        std::string site;
        double util = 0.0;
        unsigned long long remaining = 0;
    };

    struct Candidate {
        std::string filename;
        std::string src_site;
        std::string dst_site;
        unsigned long long size = 0;
        double link_load = 0.0;
        double bandwidth = 0.0;
        double latency = 0.0;
        double estimated_time = 0.0;
    };

    CGSim::Policy* storage_rebalance_policy();
    CGSim::Policy* network_aware_rebalance_policy();
    CGSim::Policy* hotset_replication_policy();

    void run_storage_rebalance(
        const std::string& policy_name,
        double high_utilization_threshold,
        double low_utilization_threshold,
        int max_transfers_per_tick,
        FilePickMode file_pick,
        bool skip_if_already_replica_on_destination,
        CGSim::FileTransferDecisionMode mode
    );

    void run_network_aware_rebalance(
        const std::string& policy_name,
        double high_utilization_threshold,
        double low_utilization_threshold,
        double max_path_load,
        int max_transfers_per_tick,
        FilePickMode file_pick,
        PathMetricMode path_metric,
        CGSim::FileTransferDecisionMode mode
    );

    void run_hotset_replication(
        const std::string& policy_name,
        double hotness_threshold,
        int target_replica_count,
        int max_transfers_per_tick,
        CGSim::FileTransferDecisionMode mode
    );

    std::vector<SiteUtil> get_site_utils() const;

    bool choose_file_from_site(
        const std::string& src_site,
        const std::string& dst_site,
        unsigned long long dst_remaining,
        FilePickMode file_pick,
        bool skip_if_already_replica_on_destination,
        std::string& filename,
        unsigned long long& filesize
    );

    sg4::Link* link_between_sites(
        const std::string& src_site,
        const std::string& dst_site
    ) const;


private:
    std::mt19937 rng{1337};
};

#endif