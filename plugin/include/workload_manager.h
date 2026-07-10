#ifndef WORKLOAD_MANAGER_H
#define WORKLOAD_MANAGER_H

#include "CGSim.h"
#include <fstream>
#include <sstream>

class WORKLOAD_MANAGER {

public:
    WORKLOAD_MANAGER(){};
   ~WORKLOAD_MANAGER(){};
    Job* createJob();
    JobQueue getWorkload();


private:
   long long random_number(long long min, long long max);
   static int JOB_ID;
   
   JobQueue getWorkload_random(long max_jobs);
   JobQueue getWorkload_from_file(long max_jobs);

   std::vector<std::string> parseCSVLine(const std::string& line);
   std::string getColumn(const std::vector<std::string>& row,
                       const std::unordered_map<std::string,int>& column_map,
                       const std::string& key,
                       const std::string& default_val = "");
};


#endif //WORKLOAD_MANAGER_H
