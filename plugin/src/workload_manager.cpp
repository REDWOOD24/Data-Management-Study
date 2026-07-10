#include "workload_manager.h"
#include <random>
#include <iostream>


long long WORKLOAD_MANAGER::random_number(long long min, long long max) {
    static std::random_device rd;
    static std::mt19937_64 gen(rd());
    std::uniform_int_distribution<long long> dist(min, max);
    return dist(gen);
}

std::vector<std::string> WORKLOAD_MANAGER::parseCSVLine(const std::string& line) {
    std::vector<std::string> row;
    std::string cell;
    bool in_quotes = false;

    for (char c : line) {
        if (c == '"') {
            in_quotes = !in_quotes;
        } else if (c == ',' && !in_quotes) {
            row.push_back(cell);
            cell.clear();
        } else {
            cell += c;
        }
    }
    row.push_back(cell);

    for (auto& field : row) {
        // Remove surrounding quotes
        if (!field.empty() && field.front() == '"' && field.back() == '"') {
            field = field.substr(1, field.size() - 2);
        }
        // Remove non-printable characters
        field.erase(std::remove_if(field.begin(), field.end(),
                    [](unsigned char c) { return !std::isprint(c); }),
                    field.end());
    }
    return row;
}

// Helper function to safely get a column value
std::string WORKLOAD_MANAGER::getColumn(const std::vector<std::string>& row,
                      const std::unordered_map<std::string,int>& column_map,
                      const std::string& key,
                      const std::string& default_val)
{
    auto it = column_map.find(key);
    if (it == column_map.end() || it->second >= static_cast<int>(row.size()) || row[it->second].empty()) {
        return default_val;
    }
    return row[it->second];
}


JobQueue WORKLOAD_MANAGER::getWorkload_random(long max_jobs) {
    JobQueue jobs;

    for(int i = 1; i <= max_jobs; i++)
    {
        Job* job = new Job();

        job->jobid                 = i;
        job->creation_time         = random_number(0,300000);
        job->cores                 = random_number(1,8);
        job->flops                 = random_number(1000000,2000000);

        int number_of_input_files  = random_number(1,5);
        int number_of_output_files = 1; //random_number(1,3);

        for(int j = 0; j < number_of_input_files; j++)
        {
            auto file = std::to_string(random_number(0,29999));
            job->input_files.insert(file);
        }

        for(int j = 0; j < number_of_output_files; j++)
        {
            auto output_file_name = "output_" + std::to_string(job->jobid) + "_" +std::to_string(j) + ".root";
            auto output_file_size = random_number(200000000000,300000000000);
            job->output_files[output_file_name] = output_file_size;
        }

        jobs.push(job);
    }

    return jobs;
}

JobQueue WORKLOAD_MANAGER::getWorkload_from_file(long max_jobs) {

    auto platform = sg4::Engine::get_instance()->get_netzone_root();
    std::string jobFile = platform->get_property("jobs_file");

    std::ifstream file(jobFile);
    if (!file.is_open()) {
        throw std::runtime_error("Could not open file: " + jobFile);
    }

    JobQueue jobs;
    std::string line;
    std::unordered_map<std::string, int> column_map;
    bool header_parsed = false;
    long jobs_with_inputs = 0;
    long total_input_files = 0;

    while (std::getline(file, line)) {
        auto row = parseCSVLine(line);

        if (!header_parsed) {
            header_parsed = true;
            for (int i = 0; i < static_cast<int>(row.size()); ++i) {
                std::string col = row[i];
                // lowercase header names
                std::transform(col.begin(), col.end(), col.begin(), ::tolower);
                column_map[col] = i;
            }
            continue;
        }

        if (max_jobs != -1 && static_cast<long>(jobs.size()) >= max_jobs) {
            break;
        }

        try {
            Job* job = new Job();

            // Safely get each column, providing defaults if missing
            job->jobid                 = std::stoll(getColumn(row, column_map, "pandaid", "0"));
            job->creation_time         = std::stoll(getColumn(row, column_map, "creationtime", ""));
            //job->job_status            = getColumn(row, column_map, "jobstatus", "");
            //job->job_name              = getColumn(row, column_map, "jobname", "");
            job->cpu_consumption_time  = std::stod(getColumn(row, column_map, "cpuconsumptiontime", "0"));
            job->comp_site             = "AGLT2_site_"+getColumn(row, column_map, "computingsite", "");
            //job->destination_dataset_name = getColumn(row, column_map, "destinationdblock", "");
            //job->destination_SE        = getColumn(row, column_map, "destinationse", "");
            //job->source_site           = getColumn(row, column_map, "sourcesite", "");
            //job->transfer_type         = getColumn(row, column_map, "transfertype", "");
            //job->core_count            = getColumn(row, column_map, "corecount", "0").empty() ? 0 : std::stoi(getColumn(row, column_map, "corecount", "0"));
            job->cores                 = getColumn(row, column_map, "corecount", "0").empty() ? 0 : std::stoi(getColumn(row, column_map, "corecount", "0"));
            //job->no_of_inp_files       = std::stoi(getColumn(row, column_map, "ninputdatafiles", "0"));
            //job->inp_file_bytes        = std::stod(getColumn(row, column_map, "inputfilebytes", "0"));
            //job->no_of_out_files       = std::stoi(getColumn(row, column_map, "noutputdatafiles", "0"));
            //job->out_file_bytes        = std::stod(getColumn(row, column_map, "outputfilebytes", "0"));
            //job->pilot_error_code      = getColumn(row, column_map, "piloterrorcode", "");
            //job->exe_error_code        = getColumn(row, column_map, "exeerrorcode", "");
            //job->ddm_error_code        = getColumn(row, column_map, "ddmerrorcode", "");
            //job->dispatcher_error_code = getColumn(row, column_map, "jobdispatchererrorcode", "");
            //job->taskbuffer_error_code = getColumn(row, column_map, "taskbuffererrorcode", "");
            job->status                = "created";
            job->retries                = 0;

            auto site = sg4::Engine::get_instance()->netzone_by_name_or_null(job->comp_site);
            if (site) {
                job->flops = std::stol(site->get_property("GFLOPS")) * job->cpu_consumption_time * job->cores;
            }

            // ---- Parse input files JSON ----
            std::string json_str = getColumn(row, column_map, "files_info", "");
            if (!json_str.empty() && json_str.front() == '"' && json_str.back() == '"') {
                json_str = json_str.substr(1, json_str.size()-2);
            }
            json_str.erase(std::remove(json_str.begin(), json_str.end(), '{'), json_str.end());
            json_str.erase(std::remove(json_str.begin(), json_str.end(), '}'), json_str.end());

            std::stringstream ss(json_str);
            std::string token;
            while (std::getline(ss, token, ',')) {
                auto colon_pos = token.find(':');
                if (colon_pos != std::string::npos) {
                    std::string key = token.substr(0, colon_pos);
                    key.erase(std::remove_if(key.begin(), key.end(), ::isspace), key.end());
                    key.erase(std::remove(key.begin(), key.end(), '"'), key.end());
                    job->input_files.insert(key);
                }
            }

            double no_of_out_files       = std::stoi(getColumn(row, column_map, "noutputdatafiles", "0"));
            double out_file_bytes        = std::stod(getColumn(row, column_map, "outputfilebytes", "0"));

            // ---- Generate output files ----
            long long size_per_out_file = no_of_out_files > 0 ? out_file_bytes / no_of_out_files : 0;
            for (int f = 1; f <= no_of_out_files; ++f) {
                std::string filename = "user.output." + std::to_string(job->jobid) + ".0000" + std::to_string(f) + ".root";
                job->output_files[filename] = size_per_out_file;
            }

            if (!job->input_files.empty()) {
                jobs_with_inputs++;
                total_input_files += job->input_files.size();
            }

            jobs.push(job);
        } catch (const std::exception& e) {
            std::cerr << "Skipping invalid row: " << line << "\n";
            std::cerr << "Reason: " << e.what() << "\n";
        }
    }
    file.close();

    std::cout << "[workload] loaded " << jobs.size() << " jobs from " << jobFile
              << " | jobs with input files: " << jobs_with_inputs
              << " | avg input files/job: "
              << (jobs_with_inputs ? static_cast<double>(total_input_files) / jobs_with_inputs : 0.0)
              << std::endl;
    std::cout << "[workload] note: creationtime is parsed with stoll(); date strings like "
              << "\"1/27/2025 2:51\" become t=1, so all jobs may submit at once"
              << std::endl;
    std::cout.flush();

    return jobs;
}

JobQueue WORKLOAD_MANAGER::getWorkload() {

    sg4::NetZone* platform = sg4::Engine::get_instance()->get_netzone_root();
    long max_jobs = std::stol(platform->get_property("Num_of_Jobs"));
    if(max_jobs > 0) {
        return getWorkload_random(max_jobs);
    }else{ // a invalid job count indicates using input file instead
        return getWorkload_from_file(max_jobs);
    }
}
