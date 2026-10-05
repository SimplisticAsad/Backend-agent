# ac.ticket.change_status  (requirements: requirement.ticket_status; operations: operation.ticket.change_status)
Feature: Change ticket status succeeds

  Scenario: Change ticket status succeeds
    Given the user is signed in as Agent or Manager
    And the record being worked on exists
    When Choose the new status
    And Change ticket status
    Then The ticket shows its new status
