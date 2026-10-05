# ac.ticket.assign  (requirements: requirement.ticket_assignment; operations: operation.user.list_agents, operation.ticket.assign)
Feature: Assign ticket succeeds

  Scenario: Assign ticket succeeds
    Given the user is signed in as Manager
    And the record being worked on exists
    When List agents
    And Pick an agent
    And Assign ticket
    Then The ticket shows its new assignee
