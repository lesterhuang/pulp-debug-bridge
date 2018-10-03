/*
 * Copyright (C) 2018 ETH Zurich, University of Bologna and GreenWaves Technologies SA
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/* 
 * Authors: Martin Croome, GWT (martin.croome@greenwaves.technologies.com)
 */


#include "bridge-commands.hpp"

int BridgeCommands::start_bridge() {
    if (m_command_stack.size() != 1) throw BridgeUnmatchedLoopException();
    queue_next_command();
    return this->m_return_value;
}

void BridgeCommands::queue_next_command() {
    m_bridge_loop->getTimerEvent([this](){
        if (m_command_stack.size() == 0) return kEventLoopTimerDone;
        auto bridge_command_collection = m_command_stack.top();
        return bridge_command_collection->execute(this->shared_from_this());
    }, 0);
    m_bridge_loop->start();
}

void BridgeCommands::stop_bridge() {
    m_bridge_loop->stop();
}

void BridgeCommands::add_execute(const bridge_func_t& cb) {
    m_command_stack.top()->add_command(std::make_shared<BridgeCommandExecute>(cb));
}

void BridgeCommands::add_repeat_start(std::chrono::microseconds delay, int count) {
    auto cloop = std::make_shared<BridgeCommandRepeat>(delay, count);
    // add the loop command to the current series of commands
    m_command_stack.top()->add_command(cloop);
    // add further commands to the loop
    m_command_stack.push(cloop);
}

void BridgeCommands::add_repeat_end() {
    if (m_command_stack.size() <= 1) throw BridgeUnmatchedLoopException();
    // finished with that loop
    m_command_stack.pop();
}

void BridgeCommands::add_delay(std::chrono::microseconds usecs) {
    m_command_stack.top()->add_command(std::make_shared<BridgeCommandDelay>(usecs));
}

void BridgeCommands::add_wait_exit(const std::shared_ptr<Ioloop> &ioloop) {
    m_command_stack.top()->add_command(std::make_shared<BridgeCommandWaitExit>(ioloop));
}

// Command execution

int64_t BridgeCommands::BridgeCommandExecute::execute(SpBridgeCommands bc) {
    printf("before execute\n");
    bc->m_return_value = m_cb(reinterpret_cast<void *>(bc.get()));
    printf("after execute %d\n", bc->m_return_value);
    return (bc->m_return_value?0:kEventLoopTimerDone);
}

int64_t BridgeCommands::BridgeCommandCollection::execute(SpBridgeCommands bc) {
    // If there are no commands left then pop the command collection
    if (m_command_queue.size() == 0) {
        bc->m_command_stack.pop();
        return 0;
    }
    // get the next command
    auto command = m_command_queue.front();
    m_command_queue.pop();
    // execute the command in the collection
    return command->execute(bc);    
}

int64_t BridgeCommands::BridgeCommandRepeat::execute(SpBridgeCommands bc) {
    // If there are no commands left then pop the command collection (loop)
    if ((m_repeat_times--) <= 0) {
        bc->m_command_stack.pop();
    } else {
        // push a collection which is a copy of the commands onto the command stack
        bc->m_command_stack.push(std::make_shared<BridgeCommandCollection>(this->m_command_queue));
    }
    return m_delay.count();
}

int64_t BridgeCommands::BridgeCommandWaitExit::execute(SpBridgeCommands bc) {
    m_ioloop->on_exit([bc](int UNUSED(status)){
        bc->queue_next_command();
    });
    return kEventLoopTimerDone;
}
