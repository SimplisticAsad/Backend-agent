# ac.ticket.update  (requirements: requirement.ticket_management; operations: operation.ticket.update)
Feature: Update Ticket succeeds

  Scenario: Update Ticket succeeds
    Given the user is signed in as Agent or Manager
    And the record being worked on exists
    When Change the Ticket details
    And Update Ticket
    Then The changes to the Ticket are saved
