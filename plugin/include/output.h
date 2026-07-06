#ifndef OUTPUT_H
#define OUTPUT_H

#include <iostream>
#include <string>
#include <stdexcept>
#include <sqlite3.h>
#include <vector>
#include <sstream>
#include "CGSim.h"
#include <nlohmann/json.hpp>
using json = nlohmann::json;

class OUTPUT {

public:
     OUTPUT(){initialize();};
    ~OUTPUT() {sqlite3_close_v2(db);}

    void initialize();
    void createEventsTable();
    void insert_event(
                  const std::string& event,
                  const std::string& state,
                  const std::string& job_id,
                  const std::string& status,
                  double time,
                  const std::string& payload);


    void onSimulationStart();
    void onSimulationEnd();
    void onJobExecutionStart(Job* job, simgrid::s4u::Exec const& ex);
    void onJobExecutionEnd(Job* job, simgrid::s4u::Exec const& ex);
    void onJobTransferStart(Job* job, simgrid::s4u::Mess const& me);
    void onJobTransferEnd(Job* job, simgrid::s4u::Mess const& me);
    void onFileTransferStart(Job* job, const std::string& filename, const unsigned long long filesize, simgrid::s4u::Comm const& co, const std::string& src_site, const std::string& dst_site);
    void onFileTransferEnd(Job* job, const std::string& filename, const unsigned long long filesize, simgrid::s4u::Comm const& co, const std::string& src_site, const std::string& dst_site);
    void onFileReadStart(Job* job, const std::string& filename, const unsigned long long filesize, simgrid::s4u::Io const& io);
    void onFileReadEnd(Job* job, const std::string& filename, const unsigned long long filesize, simgrid::s4u::Io const& io);
    void onFileWriteStart(Job* job, const std::string& filename, const unsigned long long filesize, simgrid::s4u::Io const& io);
    void onFileWriteEnd(Job* job, const std::string& filename, const unsigned long long filesize, simgrid::s4u::Io const& io);

    void onBackGroundFileTransferStart(const std::string& filename, const unsigned long long filesize, simgrid::s4u::Comm const& co, const std::string& src_site, const std::string& dst_site, const std::string& policy_name);
    void onBackGroundFileTransferEnd(const std::string& filename, const unsigned long long filesize, simgrid::s4u::Comm const& co, const std::string& src_site, const std::string& dst_site, const std::string& policy_name);


    double calculate_grid_cpu_util();
    double calculate_site_cpu_util(const std::string& site_name);
    double calculate_grid_storage_util();
    double calculate_site_storage_util(const std::string& site_name);
    sg4::Link* get_link(const std::string& src_site, const std::string& dst_site);


private:
    bool initialized = false;
    sqlite3 *db;
    sg4::NetZone* platform = sg4::Engine::get_instance()->get_netzone_root();
};

#endif
//OUTPUT_H
